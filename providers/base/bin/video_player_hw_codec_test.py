#!/usr/bin/env python3
# Copyright 2026 Canonical Ltd.
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
"""Exercise VAAPI hardware video decode in a desktop video player.

Plays a sample from the ``media-samples`` snap in a GStreamer based desktop
player (Totem, the default player up to Ubuntu 24.04, or Showtime, the
default from Ubuntu 26.04) for a fixed time. The player is run exactly as a
user would run it (``<player> <file>``), so the test checks that the stock
player configuration picks the hardware decoder. The base directory holding
the samples can be overridden with the ``MEDIA_SAMPLES_PATH`` environment
variable.

Both players are single instance applications: if one is already running,
the file is handed over to it and the command returns straight away, which
is reported as a failure. They also need a graphical (Wayland or X11)
session.

Hardware decode is confirmed with the libva tracing facility
(``LIBVA_TRACE``): trace files must show the expected VAProfile/VAEntrypoint
pair and at least ``--min-frames`` decoded pictures.
"""

import argparse
import glob
import os
import re
import signal
import subprocess
import sys
import tempfile

DEFAULT_SAMPLES_PATH = "/snap/media-samples/current/media-samples"
# How many seconds to let the player play the sample for.
DEFAULT_DURATION = 20
# Minimum number of pictures that must go through the hardware decoder.
DEFAULT_MIN_FRAMES = 30

# Variables set by the Checkbox snap runtime that would make the system
# player load the snap's libraries, Python modules or GStreamer plugins.
SNAP_RUNTIME_VARIABLES = (
    "LD_LIBRARY_PATH",
    "PYTHONPATH",
    "PYTHONHOME",
    "GI_TYPELIB_PATH",
    "GIO_MODULE_DIR",
    "GST_PLUGIN_PATH",
    "GST_PLUGIN_SYSTEM_PATH",
    "GST_PLUGIN_SCANNER",
)


def samples_root():
    """Return the directory that contains the media sample files."""
    return os.environ.get("MEDIA_SAMPLES_PATH", DEFAULT_SAMPLES_PATH)


def player_env(trace_prefix):
    """Return the environment used to run the player."""
    env = dict(os.environ)
    for name in SNAP_RUNTIME_VARIABLES:
        env.pop(name, None)
    env["LIBVA_TRACE"] = trace_prefix
    return env


def run_player(command, env, duration):
    """Run the player for ``duration`` seconds, then stop it.

    Return the exit code if the player stopped on its own before the time
    was up (a crash, a missing display or another instance already
    running), or None if it had to be stopped, which is the expected
    outcome.
    """
    print("+ {}".format(" ".join(command)))
    process = subprocess.Popen(command, env=env, start_new_session=True)
    try:
        return process.wait(timeout=duration)
    except subprocess.TimeoutExpired:
        pass
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(process.pid, sig)
        except ProcessLookupError:
            break
        try:
            process.wait(timeout=grace)
            break
        except subprocess.TimeoutExpired:
            continue
    return None


def decode_traces(trace_prefix):
    """Return the contents of the trace files that decoded pictures.

    libva appends the pid (and thread id) to the configured trace file
    name; files without any decoded picture are ignored.
    """
    traces = []
    for path in sorted(glob.glob(trace_prefix + "*")):
        with open(path, "r", errors="replace") as handle:
            text = handle.read()
        if count_pictures(text):
            traces.append(text)
    return traces


def count_pictures(trace_text):
    """Return the number of pictures submitted to the decoder."""
    return len(re.findall(r"vaEndPicture", trace_text))


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


def evaluate(traces, profile, entrypoint, min_frames):
    """Check the decode traces and return True on success."""
    matching = [
        text
        for text in traces
        if hw_acceleration_used(text, profile, entrypoint)
    ]
    if not matching:
        print(
            "---- [FAIL] not using HW decode (expected profile={} "
            "entrypoint={})".format(profile, entrypoint)
        )
        return False
    print("---- [PASS] using HW decode")
    pictures = sum(count_pictures(text) for text in matching)
    if pictures < min_frames:
        print(
            "---- [FAIL] only {} pictures decoded in HW, expected at least "
            "{}".format(pictures, min_frames)
        )
        return False
    print("---- [PASS] {} pictures decoded in HW".format(pictures))
    return True


def has_display():
    """Return True if a Wayland or X11 display is available."""
    return bool(os.environ.get("WAYLAND_DISPLAY") or os.environ.get("DISPLAY"))


def perform_test(args):
    """Run one decode test and return 0 on success, 1 on failure."""
    input_file = os.path.join(samples_root(), args.input)
    if not os.path.exists(input_file):
        print("[FAIL] input sample not found: {}".format(input_file))
        return 1
    if not has_display():
        print(
            "[FAIL] no graphical session: {} needs a Wayland or X11 "
            "display (WAYLAND_DISPLAY/DISPLAY unset)".format(args.player)
        )
        return 1

    with tempfile.TemporaryDirectory(prefix="checkbox-va-") as workdir:
        trace_prefix = os.path.join(workdir, "libva.trace")
        status = run_player(
            [args.player, input_file],
            player_env(trace_prefix),
            args.duration,
        )
        ok = evaluate(
            decode_traces(trace_prefix),
            args.profile,
            args.entrypoint,
            args.min_frames,
        )

    if status is not None:
        print(
            "---- [FAIL] {} exited early with status {} (is another "
            "instance already running?)".format(args.player, status)
        )
        return 1
    print("---- [PASS] {} ran until stopped".format(args.player))
    return 0 if ok else 1


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Test VAAPI hardware video decode in a video player."
    )
    parser.add_argument(
        "player",
        help="video player executable to run (e.g. totem or showtime)",
    )
    parser.add_argument(
        "input",
        help="sample file, relative to MEDIA_SAMPLES_PATH",
    )
    parser.add_argument(
        "--profile",
        required=True,
        help="expected VAProfile value (regex fragment, e.g. '7' or '(6|8)')",
    )
    parser.add_argument(
        "--entrypoint",
        default="1",
        help="expected VAEntrypoint value (regex fragment, default: 1)",
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=DEFAULT_DURATION,
        help="seconds to play the sample for (default: %(default)s)",
    )
    parser.add_argument(
        "--min-frames",
        type=int,
        default=DEFAULT_MIN_FRAMES,
        help="minimum pictures decoded in HW (default: %(default)s)",
    )
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return perform_test(args)


if __name__ == "__main__":
    sys.exit(main())
