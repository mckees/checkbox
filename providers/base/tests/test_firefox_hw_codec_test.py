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
import subprocess
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import firefox_hw_codec_test as m

DECODE_TRACE = (
    "[1.0][ctx none]==========va_TraceCreateConfig\n"
    "[1.0][ctx none]\tprofile = 7, VAProfileH264High\n"
    "[1.0][ctx none]\tentrypoint = 1, VAEntrypointVLD\n"
    + "[2.0][ctx 0x1]==========vaEndPicture\n" * 40
)
PROBE_TRACE = (
    "[1.0][ctx none]\tprofile = 7, VAProfileH264High\n"
    "[1.0][ctx none]\tentrypoint = 1, VAEntrypointVLD\n"
)


class TestMakeWorkdir(unittest.TestCase):
    def test_uses_snap_user_data(self):
        with tempfile.TemporaryDirectory() as home:
            with patch.dict(os.environ, {"HOME": home}):
                workdir = m.make_workdir()
            self.assertTrue(
                workdir.startswith(
                    os.path.join(home, "snap", "firefox", "common")
                )
            )
            self.assertTrue(os.path.isdir(workdir))


class TestFiles(unittest.TestCase):
    def test_write_profile(self):
        with tempfile.TemporaryDirectory() as tmp:
            profile = os.path.join(tmp, "profile")
            m.write_profile(profile)
            with open(os.path.join(profile, "user.js")) as handle:
                prefs = handle.read()
        self.assertIn('user_pref("media.autoplay.default", 0);', prefs)

    def test_write_page(self):
        with tempfile.TemporaryDirectory() as tmp:
            page = os.path.join(tmp, "play.html")
            m.write_page(page, "a.mp4")
            with open(page) as handle:
                html = handle.read()
        self.assertIn('src="a.mp4"', html)
        self.assertIn("autoplay muted loop", html)


class TestFirefoxEnvAndCommand(unittest.TestCase):
    def test_env(self):
        with patch.dict(os.environ, {"LD_LIBRARY_PATH": "/snap/x/lib"}):
            env = m.firefox_env("/w/libva.trace")
        self.assertNotIn("LD_LIBRARY_PATH", env)
        self.assertEqual(env["LIBVA_TRACE"], "/w/libva.trace")
        self.assertEqual(env["MOZ_DISABLE_RDD_SANDBOX"], "1")

    def test_command(self):
        self.assertEqual(
            m.build_firefox_command("/w/profile", "/w/play.html"),
            [
                "/snap/bin/firefox",
                "--new-instance",
                "--no-remote",
                "--profile",
                "/w/profile",
                "file:///w/play.html",
            ],
        )


class TestRunFirefox(unittest.TestCase):
    @patch("firefox_hw_codec_test.os.killpg")
    @patch("firefox_hw_codec_test.subprocess.Popen")
    def test_early_exit_returns_status(self, popen, killpg):
        popen.return_value.wait.return_value = 1
        self.assertEqual(m.run_firefox(["firefox"], {}, 5), 1)
        killpg.assert_not_called()

    @patch("firefox_hw_codec_test.os.killpg")
    @patch("firefox_hw_codec_test.subprocess.Popen")
    def test_timeout_stops_process_group(self, popen, killpg):
        process = popen.return_value
        process.pid = 42
        process.wait.side_effect = [
            subprocess.TimeoutExpired("firefox", 5),
            0,
        ]
        self.assertIsNone(m.run_firefox(["firefox"], {}, 5))
        killpg.assert_called_once_with(42, m.signal.SIGTERM)

    @patch("firefox_hw_codec_test.os.killpg")
    @patch("firefox_hw_codec_test.subprocess.Popen")
    def test_escalates_to_sigkill(self, popen, killpg):
        process = popen.return_value
        process.pid = 42
        process.wait.side_effect = [
            subprocess.TimeoutExpired("firefox", 5),
            subprocess.TimeoutExpired("firefox", 10),
            0,
        ]
        self.assertIsNone(m.run_firefox(["firefox"], {}, 5))
        self.assertEqual(
            [c[0][1] for c in killpg.call_args_list],
            [m.signal.SIGTERM, m.signal.SIGKILL],
        )

    @patch("firefox_hw_codec_test.os.killpg", side_effect=ProcessLookupError)
    @patch("firefox_hw_codec_test.subprocess.Popen")
    def test_already_gone(self, popen, _killpg):
        popen.return_value.wait.side_effect = subprocess.TimeoutExpired(
            "firefox", 5
        )
        self.assertIsNone(m.run_firefox(["firefox"], {}, 5))


class TestTraces(unittest.TestCase):
    def test_decode_traces_skips_probe(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = os.path.join(tmp, "libva.trace")
            with open(prefix + ".1.thd-1", "w") as handle:
                handle.write(PROBE_TRACE)
            with open(prefix + ".2.thd-2", "w") as handle:
                handle.write(DECODE_TRACE)
            self.assertEqual(m.decode_traces(prefix), [DECODE_TRACE])

    def test_count_pictures(self):
        self.assertEqual(m.count_pictures(DECODE_TRACE), 40)
        self.assertEqual(m.count_pictures(PROBE_TRACE), 0)

    def test_hw_acceleration_used(self):
        self.assertTrue(m.hw_acceleration_used(DECODE_TRACE, "7", "1"))
        self.assertTrue(m.hw_acceleration_used(DECODE_TRACE, "(6|7)", "1"))
        self.assertFalse(m.hw_acceleration_used(DECODE_TRACE, "32", "1"))
        self.assertFalse(
            m.hw_acceleration_used("profile = 17\nentrypoint = 1\n", "1", "1")
        )


class TestEvaluate(unittest.TestCase):
    def test_pass(self):
        self.assertTrue(m.evaluate([DECODE_TRACE], "7", "1", 30))

    def test_no_traces(self):
        self.assertFalse(m.evaluate([], "7", "1", 30))

    def test_wrong_profile(self):
        self.assertFalse(m.evaluate([DECODE_TRACE], "32", "1", 30))

    def test_too_few_pictures(self):
        self.assertFalse(m.evaluate([DECODE_TRACE], "7", "1", 100))


class TestHasDisplay(unittest.TestCase):
    def test_wayland(self):
        with patch.dict(os.environ, {"WAYLAND_DISPLAY": "wayland-0"}):
            self.assertTrue(m.has_display())

    def test_x11(self):
        env = {"DISPLAY": ":0"}
        with patch.dict(os.environ, env, clear=True):
            self.assertTrue(m.has_display())

    def test_none(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertFalse(m.has_display())


class TestPerformTest(unittest.TestCase):
    def _args(self, **kw):
        base = dict(
            input="h264/a.mp4",
            profile="7",
            entrypoint="1",
            duration=1,
            min_frames=30,
        )
        base.update(kw)
        return MagicMock(**base)

    def test_missing_input_fails(self):
        with patch.object(m.os.path, "exists", return_value=False):
            self.assertEqual(m.perform_test(self._args()), 1)

    def test_missing_firefox_fails(self):
        with tempfile.TemporaryDirectory() as tmp:
            open(os.path.join(tmp, "a.mp4"), "w").close()
            with patch.dict(os.environ, {"MEDIA_SAMPLES_PATH": tmp}), patch(
                "firefox_hw_codec_test.FIREFOX", os.path.join(tmp, "missing")
            ), patch("firefox_hw_codec_test.has_display") as display:
                self.assertEqual(m.perform_test(self._args(input="a.mp4")), 1)
            display.assert_not_called()

    @patch("firefox_hw_codec_test.has_display", return_value=False)
    @patch("firefox_hw_codec_test.os.path.exists", return_value=True)
    def test_no_display_fails(self, _exists, _display):
        self.assertEqual(m.perform_test(self._args()), 1)

    def _run(self, run_status, traces):
        with tempfile.TemporaryDirectory() as tmp:
            os.mkdir(os.path.join(tmp, "h264"))
            with open(os.path.join(tmp, "h264", "a.mp4"), "w") as handle:
                handle.write("sample")
            workdir = os.path.join(tmp, "work")
            os.mkdir(workdir)
            copied = os.path.join(workdir, "a.mp4")
            sample_present = []
            run_firefox = MagicMock(
                side_effect=lambda *a: (
                    sample_present.append(os.path.exists(copied)) or run_status
                )
            )
            with patch.dict(os.environ, {"MEDIA_SAMPLES_PATH": tmp}), patch(
                "firefox_hw_codec_test.FIREFOX", tmp
            ), patch(
                "firefox_hw_codec_test.has_display", return_value=True
            ), patch(
                "firefox_hw_codec_test.make_workdir", return_value=workdir
            ), patch(
                "firefox_hw_codec_test.run_firefox", run_firefox
            ) as run, patch(
                "firefox_hw_codec_test.decode_traces", return_value=traces
            ):
                result = m.perform_test(self._args())
            self.assertFalse(os.path.exists(workdir))
            self.assertEqual(sample_present, [True])
            command, env, duration = run.call_args[0]
            self.assertIn("--profile", command)
            self.assertEqual(env["MOZ_DISABLE_RDD_SANDBOX"], "1")
            return result

    def test_pass(self):
        self.assertEqual(self._run(None, [DECODE_TRACE]), 0)

    def test_fail_without_hw(self):
        self.assertEqual(self._run(None, []), 1)

    def test_fail_on_early_exit(self):
        self.assertEqual(self._run(1, [DECODE_TRACE]), 1)


class TestArgs(unittest.TestCase):
    def test_defaults(self):
        args = m.parse_args(["h264/a.mp4", "--profile", "7"])
        self.assertEqual(args.entrypoint, "1")
        self.assertEqual(args.duration, m.DEFAULT_DURATION)
        self.assertEqual(args.min_frames, m.DEFAULT_MIN_FRAMES)

    def test_profile_required(self):
        with self.assertRaises(SystemExit):
            m.parse_args(["h264/a.mp4"])

    @patch("firefox_hw_codec_test.perform_test", return_value=0)
    def test_main(self, perform):
        self.assertEqual(m.main(["h264/a.mp4", "--profile", "7"]), 0)
        perform.assert_called_once()


if __name__ == "__main__":
    unittest.main()
