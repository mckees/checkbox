#!/usr/bin/env python3
# This file is part of Checkbox.
#
# Copyright 2026 Canonical Ltd.
# Written by:
#   Shane McKee <shane.mckee@canonical.com>
#
# Checkbox is free software: you can redistribute it and/or modify
# it under the terms of the GNU General Public License version 3,
# as published by the Free Software Foundation.
#
# Checkbox is distributed in the hope that it will be useful,
# but WITHOUT ANY WARRANTY; without even the implied warranty of
# MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
# GNU General Public License for more details.
#
# You should have received a copy of the GNU General Public License
# along with Checkbox.  If not, see <http://www.gnu.org/licenses/>.

import unittest
from unittest.mock import patch

import lz_host


class TestCmdGpu(unittest.TestCase):
    @patch("lz_host.has_level_zero_gpu", return_value=True)
    def test_returns_zero_when_gpu_found(self, _has):
        self.assertEqual(lz_host.cmd_gpu(), 0)

    @patch("lz_host.has_level_zero_gpu", return_value=False)
    def test_returns_one_when_no_gpu(self, _has):
        self.assertEqual(lz_host.cmd_gpu(), 1)


class TestMain(unittest.TestCase):
    @patch("lz_host.cmd_gpu", return_value=0)
    @patch("sys.argv", ["lz_host.py", "gpu"])
    def test_gpu_command_dispatches_to_cmd_gpu(self, mock_gpu):
        self.assertEqual(lz_host.main(), 0)
        mock_gpu.assert_called_once_with()

    @patch("sys.argv", ["lz_host.py"])
    def test_missing_command_returns_error(self):
        self.assertEqual(lz_host.main(), 1)

    @patch("sys.argv", ["lz_host.py", "bogus"])
    def test_unknown_command_returns_error(self):
        self.assertEqual(lz_host.main(), 1)


if __name__ == "__main__":
    unittest.main()
