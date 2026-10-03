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
"""Exercise VAAPI hardware video decode in Firefox.

The Firefox snap is used. It can only read its own snap user data, not
other snaps' files, so the media sample from the ``media-samples`` snap is
copied into ``~/snap/firefox/common`` and played from there in a muted,
looping ``<video>`` element in a fresh Firefox profile for a fixed time. The
base directory holding the samples can be overridden with the
``MEDIA_SAMPLES_PATH`` environment variable.

Firefox only enables VA-API decode once its GPU compositor is up, so this
needs a graphical (Wayland or X11) session; ``--headless`` and Xvfb always
fall back to software decode.

Hardware decode is confirmed with the libva tracing facility
(``LIBVA_TRACE``). Firefox decodes in its RDD process, whose sandbox stops
it from writing the trace, so the sandbox is disabled for the run. All
trace files are read together, since libva writes one per thread. They must
show the expected VAProfile/VAEntrypoint pair and at least one decoded
picture: Firefox probes every VA profile at start-up, so the profile alone
does not prove that anything was decoded in hardware.
"""

import argparse
import glob
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile

DEFAULT_SAMPLES_PATH = "/snap/media-samples/current/media-samples"
FIREFOX = "/snap/bin/firefox"
# How many seconds to let Firefox play the sample for.
DEFAULT_DURATION = 20

USER_PREFS = (
    ("media.autoplay.default", "0"),
    ("media.autoplay.blocking_policy", "0"),
    ("browser.shell.checkDefaultBrowser", "false"),
    ("browser.aboutwelcome.enabled", "false"),
    ("browser.startup.homepage_override.mstone", '"ignore"'),
    ("browser.sessionstore.resume_from_crash", "false"),
    ("datareporting.policy.dataSubmissionEnabled", "false"),
    ("toolkit.telemetry.reportingpolicy.firstRun", "false"),
)

PAGE_TEMPLATE = """<!DOCTYPE html>
<html>
<body style="margin:0;background:black">
<video src="{src}" autoplay muted loop style="width:100%;height:100%">
</video>
</body>
</html>
"""


def samples_root():
    """Return the directory that contains the media sample files."""
    return os.environ.get("MEDIA_SAMPLES_PATH", DEFAULT_SAMPLES_PATH)


def make_workdir():
    """Create a scratch directory the Firefox snap can read and write.

    The profile, page, sample and libva traces must all live in the
    snap's user data under ``~/snap/firefox/common``.
    """
    parent = os.path.join(os.path.expanduser("~"), "snap", "firefox", "common")
    os.makedirs(parent, exist_ok=True)
    return tempfile.mkdtemp(prefix="checkbox-va-", dir=parent)


def write_profile(profile_dir):
    """Create a Firefox profile that autoplays media without prompts."""
    os.makedirs(profile_dir, exist_ok=True)
    with open(os.path.join(profile_dir, "user.js"), "w") as handle:
        for name, value in USER_PREFS:
            handle.write('user_pref("{}", {});\n'.format(name, value))


def write_page(path, src):
    """Write the HTML page that plays the sample at ``src``."""
    with open(path, "w") as handle:
        handle.write(PAGE_TEMPLATE.format(src=src))


def firefox_env(trace_prefix):
    """Return the environment used to run Firefox."""
    env = dict(os.environ)
    # The Checkbox runtime libraries must not leak into Firefox.
    env.pop("LD_LIBRARY_PATH", None)
    env["LIBVA_TRACE"] = trace_prefix
    env["MOZ_DISABLE_RDD_SANDBOX"] = "1"
    return env


def build_firefox_command(profile_dir, page):
    """Build the command line that opens ``page`` in a new instance."""
    return [
        FIREFOX,
        "--new-instance",
        "--no-remote",
        "--profile",
        profile_dir,
        "file://{}".format(page),
    ]


def run_firefox(command, env, duration):
    """Run Firefox for ``duration`` seconds, then stop it.

    Return the exit code if Firefox stopped on its own before the time was
    up (usually a crash or a missing display), or None if it had to be
    stopped, which is the expected outcome.
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


def read_trace(trace_prefix):
    """Read and concatenate every libva trace file matching ``trace_prefix``.

    libva appends the pid (and thread id) to the configured trace file name,
    so the real files are ``<prefix>.<pid>...``.
    """
    contents = []
    for path in sorted(glob.glob(trace_prefix + "*")):
        with open(path, "r", errors="replace") as handle:
            contents.append(handle.read())
    return "\n".join(contents)


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


def evaluate(trace_text, profile, entrypoint):
    """Check the libva trace and return True on success."""
    pictures = count_pictures(trace_text)
    if not pictures or not hw_acceleration_used(
        trace_text, profile, entrypoint
    ):
        print(
            "---- [FAIL] not using HW decode (expected profile={} "
            "entrypoint={})".format(profile, entrypoint)
        )
        return False
    print("---- [PASS] using HW decode ({} pictures)".format(pictures))
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
    if not os.path.exists(FIREFOX):
        print("[FAIL] Firefox snap not found: {}".format(FIREFOX))
        return 1
    if not has_display():
        print(
            "[FAIL] no graphical session: Firefox only uses VA-API decode "
            "with a Wayland or X11 display (WAYLAND_DISPLAY/DISPLAY unset)"
        )
        return 1

    workdir = make_workdir()
    try:
        profile_dir = os.path.join(workdir, "profile")
        page = os.path.join(workdir, "play.html")
        trace_prefix = os.path.join(workdir, "libva.trace")
        sample = os.path.basename(input_file)
        shutil.copyfile(input_file, os.path.join(workdir, sample))
        write_profile(profile_dir)
        write_page(page, sample)
        status = run_firefox(
            build_firefox_command(profile_dir, page),
            firefox_env(trace_prefix),
            args.duration,
        )
        ok = evaluate(read_trace(trace_prefix), args.profile, args.entrypoint)
    finally:
        shutil.rmtree(workdir, ignore_errors=True)

    if status is not None:
        print("---- [FAIL] firefox exited early with status {}".format(status))
        return 1
    print("---- [PASS] firefox ran until stopped")
    return 0 if ok else 1


def parse_args(argv):
    parser = argparse.ArgumentParser(
        description="Test VAAPI hardware video decode in Firefox."
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
    return parser.parse_args(argv)


def main(argv=None):
    args = parse_args(argv if argv is not None else sys.argv[1:])
    return perform_test(args)


if __name__ == "__main__":
    sys.exit(main())
