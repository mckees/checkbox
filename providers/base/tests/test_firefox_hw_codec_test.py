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

import os
import tempfile
import unittest
from unittest.mock import patch

import firefox_hw_codec_test as m


def write(path, text):
    with open(path, "w") as f:
        f.write(text)


class TestPrepare(unittest.TestCase):
    def test_prepare(self):
        with tempfile.TemporaryDirectory() as tmp:
            sample = os.path.join(tmp, "a.mp4")
            write(sample, "video")
            work = os.path.join(tmp, "work")
            os.mkdir(work)
            page, profile = m.prepare(work, sample)
            self.assertTrue(os.path.isfile(os.path.join(work, "a.mp4")))
            with open(page) as f:
                self.assertIn('src="a.mp4" autoplay muted loop', f.read())
            with open(os.path.join(profile, "user.js")) as f:
                self.assertIn("checkDefaultBrowser", f.read())


class TestRunFirefox(unittest.TestCase):
    @patch.dict(os.environ, {"LD_LIBRARY_PATH": "/snap/checkbox/lib"})
    @patch("firefox_hw_codec_test.subprocess.call", return_value=124)
    def test_timeout_is_expected(self, call):
        self.assertTrue(m.run_firefox("/w", "/w/play.html", "/w/profile"))
        command = call.call_args[0][0]
        self.assertEqual(command[:3], ["timeout", "20s", "/snap/bin/firefox"])
        self.assertIn("file:///w/play.html", command)
        env = call.call_args[1]["env"]
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertEqual(env["LIBVA_TRACE"], "/w/libva.trace")
        self.assertEqual(env["MOZ_DISABLE_RDD_SANDBOX"], "1")

    @patch("firefox_hw_codec_test.subprocess.call", return_value=1)
    def test_early_exit_fails(self, _call):
        self.assertFalse(m.run_firefox("/w", "/w/play.html", "/w/profile"))


class TestHwDecodeUsed(unittest.TestCase):
    def check(self, traces):
        with tempfile.TemporaryDirectory() as tmp:
            for name, text in traces.items():
                write(os.path.join(tmp, name), text)
            return m.hw_decode_used(tmp)

    def test_pictures_across_threads(self):
        first = m.MIN_PICTURES // 2
        traces = {
            "libva.trace.1.thd-1": "vaEndPicture\n" * first,
            "libva.trace.1.thd-2": "vaEndPicture\n" * (m.MIN_PICTURES - first),
        }
        self.assertTrue(self.check(traces))

    def test_fallback_after_a_few_pictures(self):
        trace = "vaEndPicture\n" * (m.MIN_PICTURES - 1)
        self.assertFalse(self.check({"libva.trace.1": trace}))

    def test_no_trace(self):
        self.assertFalse(self.check({}))


class TestMain(unittest.TestCase):
    def test_bad_args(self):
        with self.assertRaises(SystemExit):
            m.main([])

    def test_missing_sample(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.dict(os.environ, {"MEDIA_SAMPLES_PATH": tmp}):
                self.assertEqual(m.main(["missing.mp4"]), 1)

    def run_main(self, firefox_ok, hw_used):
        with tempfile.TemporaryDirectory() as tmp:
            write(os.path.join(tmp, "a.mp4"), "video")
            env = {"MEDIA_SAMPLES_PATH": tmp, "HOME": tmp}
            with patch.dict(os.environ, env), patch(
                "firefox_hw_codec_test.run_firefox", return_value=firefox_ok
            ) as run, patch(
                "firefox_hw_codec_test.hw_decode_used", return_value=hw_used
            ):
                result = m.main(["a.mp4"])
            working_dir = run.call_args[0][0]
            self.assertTrue(
                working_dir.startswith(
                    os.path.join(tmp, "snap", "firefox", "common")
                )
            )
            self.assertFalse(os.path.exists(working_dir))
            return result

    def test_pass(self):
        self.assertEqual(self.run_main(True, True), 0)

    def test_no_hw_decode(self):
        self.assertEqual(self.run_main(True, False), 1)

    def test_firefox_error(self):
        self.assertEqual(self.run_main(False, True), 1)


if __name__ == "__main__":
    unittest.main()
