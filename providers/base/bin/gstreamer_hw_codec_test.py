#!/usr/bin/env python3
# Copyright 2024 Canonical Ltd.
# Written by:
#   Shane McKee <shane.mckee@canonical.com>
#
# This program is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3,
# as published by the Free Software Foundation.
#
# This program is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with this program.  If not, see <http://www.gnu.org/licenses/>.

"""Exercise VAAPI hardware video decode/encode with GStreamer.

The media sample files are provided by the ``media-samples`` snap and are no
longer downloaded at runtime. The base directory holding the samples can be
overridden with the ``MEDIA_SAMPLES_PATH`` environment variable; it defaults
to the snap mount point ``/snap/media-samples/current/media-samples``.

Hardware acceleration is confirmed by enabling the libva tracing facility
(``LIBVA_TRACE``) and inspecting the trace for the expected VAProfile /
VAEntrypoint pair. For encode, the output is additionally compared against
the input with ``avvideocompare`` and the resulting SSIM must stay above a
threshold, catching a hardware encoder that runs but produces garbage.
"""

import argparse
import glob
import os
import re
import subprocess
import sys
import tempfile

DEFAULT_SAMPLES_PATH = "/snap/media-samples/current/media-samples"
# Minimum acceptable SSIM between the encode input and output.
SSIM_THRESHOLD = 0.9


def samples_root():
    """Return the directory that contains the media sample files."""
    return os.environ.get("MEDIA_SAMPLES_PATH", DEFAULT_SAMPLES_PATH)


def resolve_sample(relative_path):
    """Resolve a sample path relative to the media-samples directory.

    Absolute paths are returned unchanged so the helper can also be pointed
    at an arbitrary file for local debugging.
    """
    if os.path.isabs(relative_path):
        return relative_path
    return os.path.join(samples_root(), relative_path)


def build_decode_command(input_file):
    """Build the gst-launch pipeline for a hardware decode test.

    ``decodebin`` auto-plugs the highest-ranked decoder, which is the VAAPI
    hardware decoder when gstreamer-vaapi is installed, and the decoded
    frames are dropped into a ``fakevideosink``.
    """
    return [
        "gst-launch-1.0",
        "-q",
        "filesrc",
        "location={}".format(input_file),
        "!",
        "decodebin",
        "!",
        "fakevideosink",
        "sync=false",
    ]


def build_encode_command(input_file, encoder, parser, muxer, output_file):
    """Build the gst-launch pipeline for a hardware encode test.

    The input is decoded and converted in software then fed to the VAAPI
    hardware ``encoder`` (e.g. ``vaapih264enc``), parsed and muxed into the
    output container so the test exercises the hardware *encoder* in
    isolation, independent of whether the input codec can be hardware
    *decoded*.
    """
    return [
        "gst-launch-1.0",
        "-q",
        "filesrc",
        "location={}".format(input_file),
        "!",
        "decodebin",
        "!",
        "videoconvert",
        "!",
        encoder,
        "!",
        parser,
        "!",
        muxer,
        "!",
        "filesink",
        "location={}".format(output_file),
    ]


def build_compare_command(input_file, output_file, stats_file):
    """Build the gst-launch pipeline that compares two videos with SSIM.

    Both the original input and the freshly encoded output are decoded to
    raw I420 frames and fed to ``avvideocompare``, which writes per-frame
    SSIM statistics to ``stats_file``.
    """
    return [
        "gst-launch-1.0",
        "-q",
        "filesrc",
        "location={}".format(input_file),
        "!",
        "queue",
        "!",
        "decodebin",
        "!",
        "videoconvert",
        "!",
        "video/x-raw,format=I420",
        "!",
        "queue",
        "!",
        "video_compare.sink_1",
        "filesrc",
        "location={}".format(output_file),
        "!",
        "queue",
        "!",
        "decodebin",
        "!",
        "videoconvert",
        "!",
        "video/x-raw,format=I420",
        "!",
        "queue",
        "!",
        "video_compare.sink_2",
        "avvideocompare",
        "method=ssim",
        "stats-file={}".format(stats_file),
        "name=video_compare",
        "!",
        "fakesink",
    ]


def read_trace(trace_prefix):
    """Read and concatenate every libva trace file matching ``trace_prefix``.

    libva appends the pid (and thread id) to the configured trace file name,
    so the real files are ``<prefix>.<pid>...``.
    """
    contents = []
    for path in sorted(glob.glob(trace_prefix + "*")):
        try:
            with open(path, "r", errors="replace") as handle:
                contents.append(handle.read())
        except OSError:
            continue
    return "\n".join(contents)


def hw_acceleration_used(trace_text, profile, entrypoint):
    """Return True if the trace shows the expected profile and entrypoint.

    ``profile`` and ``entrypoint`` are treated as regular expression fragments
    (e.g. ``"7"`` or ``"(6|8)"``). A profile line must be followed shortly
    after by a matching entrypoint line, mirroring the libva trace layout::

        profile = 7
        entrypoint = 1
    """
    profile_re = re.compile(r"profile\s*=\s*(?:{})\b".format(profile))
    entrypoint_re = re.compile(r"entrypoint\s*=\s*(?:{})\b".format(entrypoint))
    lines = trace_text.splitlines()
    for index, line in enumerate(lines):
        if profile_re.search(line):
            for following in lines[index + 1 : index + 4]:
                if entrypoint_re.search(following):
                    return True
    return False


def parse_min_ssim(stats_text):
    """Return the lowest ``All:`` SSIM value in ``stats_text``, or None.

    ``avvideocompare`` writes one line per frame ending with the aggregate
    metric ``All:<value>``; the weakest frame is what matters for quality.
    """
    values = [float(v) for v in re.findall(r"All:([0-9.]+)", stats_text)]
    if not values:
        return None
    return min(values)


def run_gst(command, trace_prefix):
    """Run gst-launch with libva tracing enabled and return the exit code."""
    env = dict(os.environ)
    env["LIBVA_TRACE"] = trace_prefix
    print("+ {}".format(" ".join(command)))
    result = subprocess.run(
        command,
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        universal_newlines=True,
    )
    if result.stdout:
        print(result.stdout)
    return result.returncode


def _silent_remove(path, is_dir=False):
    try:
        if is_dir:
            os.rmdir(path)
        else:
            os.remove(path)
    except OSError:
        pass


def _check_ssim(input_file, output_file, workdir, trace_prefix):
    """Compare input and output with SSIM and return True on success."""
    if not os.path.exists(output_file):
        print("---- [FAIL] no encode output produced to compare")
        return False
    stats_file = os.path.join(workdir, "ssim.log")
    try:
        command = build_compare_command(input_file, output_file, stats_file)
        run_gst(command, trace_prefix)
        stats_text = ""
        if os.path.exists(stats_file):
            with open(stats_file, "r", errors="replace") as handle:
                stats_text = handle.read()
    finally:
        _silent_remove(stats_file)

    min_ssim = parse_min_ssim(stats_text)
    if min_ssim is None:
        print("---- [FAIL] SSIM statistics not produced")
        return False
    if min_ssim >= SSIM_THRESHOLD:
        print(
            "---- [PASS] minimum SSIM ({}) is above {}".format(
                min_ssim, SSIM_THRESHOLD
            )
        )
        return True
    print(
        "---- [FAIL] minimum SSIM ({}) dropped below {}".format(
            min_ssim, SSIM_THRESHOLD
        )
    )
    return False


def perform_test(args):
    """Run one decode/encode test and return 0 on success, 1 on failure."""
    input_file = resolve_sample(args.input)
    if not os.path.exists(input_file):
        print("[FAIL] input sample not found: {}".format(input_file))
        return 1

    workdir = tempfile.mkdtemp(prefix="media_hw_codec_")
    trace_prefix = os.path.join(workdir, "libva.trace")
    ssim_ok = True

    if args.operation == "decode":
        output_file = None
        command = build_decode_command(input_file)
    else:
        output_file = os.path.join(
            workdir, "out.{}".format(args.output_container)
        )
        command = build_encode_command(
            input_file, args.encoder, args.parser, args.muxer, output_file
        )

    try:
        gst_status = run_gst(command, trace_prefix)
        trace_text = read_trace(trace_prefix)
        hw_used = hw_acceleration_used(
            trace_text, args.profile, args.entrypoint
        )
        if args.operation == "encode" and gst_status == 0:
            ssim_ok = _check_ssim(
                input_file, output_file, workdir, trace_prefix
            )
    finally:
        for path in glob.glob(trace_prefix + "*"):
            _silent_remove(path)
        if output_file is not None:
            _silent_remove(output_file)
        _silent_remove(workdir, is_dir=True)

    if hw_used:
        print("---- [PASS] using HW {}".format(args.operation))
    else:
        print(
            "---- [FAIL] not using HW {} (expected profile={} "
            "entrypoint={})".format(
                args.operation, args.profile, args.entrypoint
            )
        )

    if gst_status != 0:
        print("---- [FAIL] gstreamer returned {}".format(gst_status))
    else:
        print("---- [PASS] gstreamer command completed successfully")

    if not hw_used or gst_status != 0 or not ssim_ok:
        return 1
    return 0


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Test VAAPI hardware video decode/encode with GStreamer."
    )
    parser.add_argument(
        "operation",
        choices=("decode", "encode"),
        help="whether to test hardware decode or encode",
    )
    parser.add_argument(
        "input",
        help="sample file, relative to MEDIA_SAMPLES_PATH or absolute",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="expected VAProfile value (regex fragment, e.g. '7' or '(6|8)')",
    )
    parser.add_argument(
        "--entrypoint",
        required=True,
        help="expected VAEntrypoint value (regex fragment)",
    )
    parser.add_argument(
        "--encoder",
        help="gstreamer encode element for encode (e.g. vaapih264enc)",
    )
    parser.add_argument(
        "--parser",
        help="gstreamer parse element for encode (e.g. h264parse)",
    )
    parser.add_argument(
        "--muxer",
        help="gstreamer mux element for encode (e.g. mp4mux)",
    )
    parser.add_argument(
        "--output-container",
        default="mp4",
        help="output container extension for encode (default: mp4)",
    )
    args = parser.parse_args(argv)
    if args.operation == "encode" and not (
        args.encoder and args.parser and args.muxer
    ):
        parser.error("--encoder, --parser and --muxer are required for encode")
    return args


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return perform_test(args)


if __name__ == "__main__":
    sys.exit(main())
