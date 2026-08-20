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

import os
import unittest
from unittest.mock import patch, MagicMock

import gstreamer_hw_codec_test as m


class TestSampleResolution(unittest.TestCase):
    def test_default_samples_root(self):
        with patch.dict(os.environ, {}, clear=True):
            self.assertEqual(m.samples_root(), m.DEFAULT_SAMPLES_PATH)

    def test_samples_root_env_override(self):
        with patch.dict(os.environ, {"MEDIA_SAMPLES_PATH": "/tmp/x"}):
            self.assertEqual(m.samples_root(), "/tmp/x")

    def test_resolve_relative(self):
        with patch.dict(os.environ, {"MEDIA_SAMPLES_PATH": "/base"}):
            self.assertEqual(
                m.resolve_sample("av1/foo.mkv"), "/base/av1/foo.mkv"
            )

    def test_resolve_absolute_unchanged(self):
        self.assertEqual(m.resolve_sample("/abs/foo.mkv"), "/abs/foo.mkv")


class TestCommandBuilders(unittest.TestCase):
    def test_decode_command(self):
        cmd = m.build_decode_command("in.mkv")
        self.assertEqual(cmd[0], "gst-launch-1.0")
        self.assertIn("decodebin", cmd)
        self.assertIn("fakevideosink", cmd)
        self.assertIn("location=in.mkv", cmd)

    def test_encode_command(self):
        cmd = m.build_encode_command(
            "in.mp4", "vah264enc", "h264parse", "mp4mux", "o.mp4"
        )
        self.assertIn("decodebin", cmd)
        self.assertIn("videoconvert", cmd)
        self.assertIn("vah264enc", cmd)
        self.assertIn("h264parse", cmd)
        self.assertIn("mp4mux", cmd)
        self.assertIn("location=in.mp4", cmd)
        self.assertEqual(cmd[-1], "location=o.mp4")

    def test_compare_command(self):
        cmd = m.build_compare_command("in.mp4", "o.mp4", "s.log")
        self.assertIn("avvideocompare", cmd)
        self.assertIn("method=ssim", cmd)
        self.assertIn("stats-file=s.log", cmd)
        self.assertIn("video_compare.sink_1", cmd)
        self.assertIn("video_compare.sink_2", cmd)


class TestHwAccelerationUsed(unittest.TestCase):
    def test_matching_profile_and_entrypoint(self):
        trace = "    profile = 7\n    entrypoint = 1\n"
        self.assertTrue(m.hw_acceleration_used(trace, "7", "1"))

    def test_regex_profile_group(self):
        trace = "profile = 20\nentrypoint = 8\n"
        self.assertTrue(m.hw_acceleration_used(trace, "(19|20|21|22)", "8"))

    def test_word_boundary_prevents_partial_match(self):
        # profile 19 must not satisfy an expected profile of "1"
        trace = "profile = 19\nentrypoint = 1\n"
        self.assertFalse(m.hw_acceleration_used(trace, "1", "1"))

    def test_entrypoint_must_follow_profile(self):
        trace = "entrypoint = 1\nprofile = 7\n"
        self.assertFalse(m.hw_acceleration_used(trace, "7", "1"))

    def test_no_match_empty_trace(self):
        self.assertFalse(m.hw_acceleration_used("", "7", "1"))


class TestParseMinSsim(unittest.TestCase):
    def test_returns_minimum(self):
        stats = "frame 0 All:0.99\nframe 1 All:0.95\nframe 2 All:0.97\n"
        self.assertEqual(m.parse_min_ssim(stats), 0.95)

    def test_no_values_returns_none(self):
        self.assertIsNone(m.parse_min_ssim("nothing here"))


class TestReadTrace(unittest.TestCase):
    def test_reads_and_concatenates(self):
        import tempfile

        d = tempfile.mkdtemp()
        prefix = os.path.join(d, "libva.trace")
        with open(prefix + ".111", "w") as f:
            f.write("a")
        with open(prefix + ".222", "w") as f:
            f.write("b")
        try:
            text = m.read_trace(prefix)
            self.assertIn("a", text)
            self.assertIn("b", text)
        finally:
            for p in (prefix + ".111", prefix + ".222"):
                os.remove(p)
            os.rmdir(d)

    def test_missing_files_returns_empty(self):
        self.assertEqual(m.read_trace("/nonexistent/libva.trace"), "")


class TestArgParsing(unittest.TestCase):
    def test_encode_requires_elements(self):
        with self.assertRaises(SystemExit):
            m.parse_args(
                ["encode", "in.mp4", "--profile", "7", "--entrypoint", "1"]
            )

    def test_decode_ok(self):
        args = m.parse_args(
            ["decode", "in.mkv", "--profile", "32", "--entrypoint", "1"]
        )
        self.assertEqual(args.operation, "decode")
        self.assertEqual(args.profile, "32")

    def test_encode_ok(self):
        args = m.parse_args(
            [
                "encode",
                "in.mp4",
                "--profile",
                "(6|7|13)",
                "--entrypoint",
                "(6|8)",
                "--encoder",
                "vaapih264enc",
                "--parser",
                "h264parse",
                "--muxer",
                "mp4mux",
            ]
        )
        self.assertEqual(args.encoder, "vaapih264enc")


class TestPerformTest(unittest.TestCase):
    def _args(self, **kw):
        base = dict(
            operation="decode",
            input="av1/foo.mkv",
            profile="32",
            entrypoint="1",
            encoder=None,
            parser=None,
            muxer=None,
            output_container="mp4",
        )
        base.update(kw)
        return MagicMock(**base)

    def test_missing_input_fails(self):
        with patch.object(m.os.path, "exists", return_value=False):
            self.assertEqual(m.perform_test(self._args()), 1)

    @patch("gstreamer_hw_codec_test.read_trace")
    @patch("gstreamer_hw_codec_test.run_gst")
    @patch("gstreamer_hw_codec_test.os.path.exists", return_value=True)
    def test_pass_when_hw_used_and_gst_ok(self, _exists, run_gst, read_trace):
        run_gst.return_value = 0
        read_trace.return_value = "profile = 32\nentrypoint = 1\n"
        self.assertEqual(m.perform_test(self._args()), 0)

    @patch("gstreamer_hw_codec_test.read_trace")
    @patch("gstreamer_hw_codec_test.run_gst")
    @patch("gstreamer_hw_codec_test.os.path.exists", return_value=True)
    def test_decode_boosts_hw_decoder_rank(self, _exists, run_gst, read_trace):
        run_gst.return_value = 0
        read_trace.return_value = "profile = 32\nentrypoint = 1\n"
        m.perform_test(self._args())
        extra_env = run_gst.call_args_list[0][0][2]
        self.assertIn("vajpegdec:512", extra_env["GST_PLUGIN_FEATURE_RANK"])

    @patch("gstreamer_hw_codec_test.read_trace")
    @patch("gstreamer_hw_codec_test.run_gst")
    @patch("gstreamer_hw_codec_test.os.path.exists", return_value=True)
    def test_fail_when_hw_not_used(self, _exists, run_gst, read_trace):
        run_gst.return_value = 0
        read_trace.return_value = "nothing here"
        self.assertEqual(m.perform_test(self._args()), 1)

    @patch("gstreamer_hw_codec_test.read_trace")
    @patch("gstreamer_hw_codec_test.run_gst")
    @patch("gstreamer_hw_codec_test.os.path.exists", return_value=True)
    def test_fail_when_gst_errors(self, _exists, run_gst, read_trace):
        run_gst.return_value = 1
        read_trace.return_value = "profile = 32\nentrypoint = 1\n"
        self.assertEqual(m.perform_test(self._args()), 1)

    @patch("gstreamer_hw_codec_test._check_ssim", return_value=True)
    @patch("gstreamer_hw_codec_test.read_trace")
    @patch("gstreamer_hw_codec_test.run_gst")
    @patch("gstreamer_hw_codec_test.os.path.exists", return_value=True)
    def test_encode_pass_uses_output_container(
        self, _exists, run_gst, read_trace, check_ssim
    ):
        run_gst.return_value = 0
        read_trace.return_value = "profile = 7\nentrypoint = 8\n"
        args = self._args(
            operation="encode",
            input="av1/foo.mkv",
            encoder="vah264enc",
            parser="h264parse",
            muxer="mp4mux",
            output_container="mp4",
            profile="(6|7|13)",
            entrypoint="(6|8)",
        )
        self.assertEqual(m.perform_test(args), 0)
        built = run_gst.call_args_list[0][0][0]
        self.assertIn("vah264enc", built)
        self.assertIsNone(run_gst.call_args_list[0][0][2])

    @patch("gstreamer_hw_codec_test._check_ssim", return_value=False)
    @patch("gstreamer_hw_codec_test.read_trace")
    @patch("gstreamer_hw_codec_test.run_gst")
    @patch("gstreamer_hw_codec_test.os.path.exists", return_value=True)
    def test_encode_fail_when_ssim_low(
        self, _exists, run_gst, read_trace, check_ssim
    ):
        run_gst.return_value = 0
        read_trace.return_value = "profile = 7\nentrypoint = 8\n"
        args = self._args(
            operation="encode",
            input="av1/foo.mkv",
            encoder="vah264enc",
            parser="h264parse",
            muxer="mp4mux",
            output_container="mp4",
            profile="(6|7|13)",
            entrypoint="(6|8)",
        )
        self.assertEqual(m.perform_test(args), 1)


class TestCheckSsim(unittest.TestCase):
    def test_fails_without_output(self):
        with patch.object(m.os.path, "exists", return_value=False):
            self.assertFalse(
                m._check_ssim("in.mp4", "out.mp4", "/tmp", "/tmp/t")
            )


class TestRunGst(unittest.TestCase):
    @patch("gstreamer_hw_codec_test.subprocess.run")
    def test_sets_libva_trace_env(self, sp_run):
        sp_run.return_value = MagicMock(returncode=0, stdout="out")
        rc = m.run_gst(["gst-launch-1.0", "-q"], "/tmp/libva.trace")
        self.assertEqual(rc, 0)
        env = sp_run.call_args[1]["env"]
        self.assertEqual(env["LIBVA_TRACE"], "/tmp/libva.trace")

    @patch("gstreamer_hw_codec_test.subprocess.run")
    def test_applies_extra_env(self, sp_run):
        sp_run.return_value = MagicMock(returncode=0, stdout="out")
        m.run_gst(
            ["gst-launch-1.0", "-q"],
            "/tmp/libva.trace",
            {"GST_PLUGIN_FEATURE_RANK": "vajpegdec:512"},
        )
        env = sp_run.call_args[1]["env"]
        self.assertEqual(env["GST_PLUGIN_FEATURE_RANK"], "vajpegdec:512")


class TestHwDecoderRankEnv(unittest.TestCase):
    def test_boosts_all_hw_decoders(self):
        value = m.hw_decoder_rank_env()
        for element in m.HW_DECODER_ELEMENTS:
            self.assertIn("{}:512".format(element), value)


if __name__ == "__main__":
    unittest.main()
