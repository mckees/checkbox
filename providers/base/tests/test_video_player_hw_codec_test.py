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
from unittest.mock import patch

import video_player_hw_codec_test as m

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


class TestPlayerEnv(unittest.TestCase):
    def test_drops_snap_runtime_and_sets_trace(self):
        leaked = {name: "/snap/x" for name in m.SNAP_RUNTIME_VARIABLES}
        leaked["DISPLAY"] = ":0"
        with patch.dict(os.environ, leaked):
            env = m.player_env("/w/libva.trace")
        for name in m.SNAP_RUNTIME_VARIABLES:
            self.assertNotIn(name, env)
        self.assertEqual(env["DISPLAY"], ":0")
        self.assertEqual(env["LIBVA_TRACE"], "/w/libva.trace")


class TestRunPlayer(unittest.TestCase):
    @patch("video_player_hw_codec_test.os.killpg")
    @patch("video_player_hw_codec_test.subprocess.Popen")
    def test_early_exit_returns_status(self, popen, killpg):
        popen.return_value.wait.return_value = 0
        self.assertEqual(m.run_player(["showtime"], {}, 5), 0)
        killpg.assert_not_called()

    @patch("video_player_hw_codec_test.os.killpg")
    @patch("video_player_hw_codec_test.subprocess.Popen")
    def test_escalates_to_sigkill(self, popen, killpg):
        process = popen.return_value
        process.pid = 42
        process.wait.side_effect = [
            subprocess.TimeoutExpired("totem", 5),
            subprocess.TimeoutExpired("totem", 10),
            0,
        ]
        self.assertIsNone(m.run_player(["totem"], {}, 5))
        self.assertEqual(
            [c[0] for c in killpg.call_args_list],
            [(42, m.signal.SIGTERM), (42, m.signal.SIGKILL)],
        )


class TestTraces(unittest.TestCase):
    def test_decode_traces_skips_files_without_pictures(self):
        with tempfile.TemporaryDirectory() as tmp:
            prefix = os.path.join(tmp, "libva.trace")
            with open(prefix + ".1.thd-1", "w") as handle:
                handle.write(PROBE_TRACE)
            with open(prefix + ".2.thd-2", "w") as handle:
                handle.write(DECODE_TRACE)
            self.assertEqual(m.decode_traces(prefix), [DECODE_TRACE])

    def test_evaluate(self):
        self.assertTrue(m.evaluate([DECODE_TRACE], "7", "1", 30))
        self.assertFalse(m.evaluate([], "7", "1", 30))
        self.assertFalse(m.evaluate([DECODE_TRACE], "32", "1", 30))
        self.assertFalse(m.evaluate([DECODE_TRACE], "7", "1", 100))


class TestPerformTest(unittest.TestCase):
    def _args(self, *extra):
        return m.parse_args(
            ["totem", "h264/a.mp4", "--profile", "7"] + list(extra)
        )

    def test_missing_input_fails(self):
        with patch.dict(os.environ, {"MEDIA_SAMPLES_PATH": "/nonexistent"}):
            self.assertEqual(m.perform_test(self._args()), 1)

    @patch("video_player_hw_codec_test.run_player")
    def test_no_display_fails(self, run):
        with patch.dict(os.environ, {}, clear=True), patch(
            "video_player_hw_codec_test.os.path.exists", return_value=True
        ):
            self.assertEqual(m.perform_test(self._args()), 1)
        run.assert_not_called()

    def _run(self, run_status, traces):
        with tempfile.TemporaryDirectory() as tmp:
            env = {"MEDIA_SAMPLES_PATH": tmp, "WAYLAND_DISPLAY": "wayland-0"}
            with patch.dict(os.environ, env), patch(
                "video_player_hw_codec_test.os.path.exists", return_value=True
            ), patch(
                "video_player_hw_codec_test.run_player",
                return_value=run_status,
            ) as run, patch(
                "video_player_hw_codec_test.decode_traces",
                return_value=traces,
            ):
                result = m.perform_test(self._args())
            command, player_env, duration = run.call_args[0]
            self.assertEqual(
                command, ["totem", os.path.join(tmp, "h264/a.mp4")]
            )
            self.assertIn("LIBVA_TRACE", player_env)
            self.assertEqual(duration, m.DEFAULT_DURATION)
            return result

    def test_pass(self):
        self.assertEqual(self._run(None, [DECODE_TRACE]), 0)

    def test_fail_without_hw(self):
        self.assertEqual(self._run(None, []), 1)

    def test_fail_on_early_exit(self):
        # e.g. the file was handed over to an already running instance
        self.assertEqual(self._run(0, [DECODE_TRACE]), 1)


if __name__ == "__main__":
    unittest.main()
