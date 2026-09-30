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
"""Play a media-samples video in the Firefox snap for 20 seconds and check
the libva trace to confirm it was decoded in hardware.
"""

import argparse
import glob
import os
import shutil
import subprocess
import sys
import tempfile

DEFAULT_SAMPLES_PATH = "/snap/media-samples/current/media-samples"
# 20 seconds of HW playback decodes hundreds of pictures. A handful means
# Firefox decoded a few frames in HW and then fell back to software.
MIN_PICTURES = 30

PAGE = """<!DOCTYPE html>
<html><body style="margin:0;background:black">
<video src="{}" autoplay muted loop style="width:100%;height:100%"></video>
</body></html>
"""

USER_JS = """user_pref("browser.shell.checkDefaultBrowser", false);
user_pref("browser.aboutwelcome.enabled", false);
user_pref("browser.startup.homepage_override.mstone", "ignore");
user_pref("datareporting.policy.dataSubmissionEnabled", false);
"""


def prepare(working_dir, sample):
    """Copy the sample, page and profile into working_dir."""
    shutil.copy(sample, working_dir)
    page = os.path.join(working_dir, "play.html")
    with open(page, "w") as f:
        f.write(PAGE.format(os.path.basename(sample)))
    profile = os.path.join(working_dir, "profile")
    os.mkdir(profile)
    with open(os.path.join(profile, "user.js"), "w") as f:
        f.write(USER_JS)
    return page, profile


def run_firefox(working_dir, page, profile):
    """Return True if Firefox played the page until the 20s timeout."""
    env = dict(os.environ)
    # Checkbox runtime libraries must not leak into Firefox, and Firefox's
    # decoder (RDD) sandbox would stop libva from writing its trace.
    env.pop("LD_LIBRARY_PATH", None)
    env["LIBVA_TRACE"] = os.path.join(working_dir, "libva.trace")
    env["MOZ_DISABLE_RDD_SANDBOX"] = "1"
    return_status = subprocess.call(
        [
            "timeout",
            "20s",
            "/snap/bin/firefox",
            "--new-instance",
            "--profile",
            profile,
            "file://" + page,
        ],
        env=env,
    )
    # 124 means timeout stopped Firefox, as intended
    if return_status == 124:
        print("---- [PASS] firefox command completed successfully")
        return True
    print("---- [FAIL] firefox returned an error not related to timing out")
    return False


def hw_decode_used(working_dir):
    """Return True if the libva traces show at least MIN_PICTURES decoded.

    Unlike in the ffmpeg and gstreamer tests, the VAProfile/VAEntrypoint
    trace lines prove nothing: Firefox's start-up probe creates a VA config
    for every supported profile. libva writes one trace per thread.
    """
    traces = ""
    for path in glob.glob(os.path.join(working_dir, "libva.trace*")):
        with open(path, errors="replace") as f:
            traces += f.read()
    pictures = traces.count("vaEndPicture")
    if pictures >= MIN_PICTURES:
        print("---- [PASS] using HW decode ({} pictures)".format(pictures))
        return True
    print(
        "---- [FAIL] not using HW decode (expected at least {} pictures, "
        "got {})".format(MIN_PICTURES, pictures)
    )
    return False


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("sample", help="path relative to MEDIA_SAMPLES_PATH")
    args = parser.parse_args(argv)

    samples = os.environ.get("MEDIA_SAMPLES_PATH", DEFAULT_SAMPLES_PATH)
    sample = os.path.join(samples, args.sample)
    if not os.path.isfile(sample):
        print("---- [FAIL] input sample not found: {}".format(sample))
        return 1

    # The Firefox snap can only read its own user data
    snap_data = os.path.expanduser("~/snap/firefox/common")
    os.makedirs(snap_data, exist_ok=True)
    working_dir = tempfile.mkdtemp(prefix="checkbox-va-", dir=snap_data)
    try:
        page, profile = prepare(working_dir, sample)
        firefox_ok = run_firefox(working_dir, page, profile)
        hw_used = hw_decode_used(working_dir)
    finally:
        shutil.rmtree(working_dir, ignore_errors=True)
    return 0 if firefox_ok and hw_used else 1


if __name__ == "__main__":
    sys.exit(main())
