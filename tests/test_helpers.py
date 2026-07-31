# -*- coding: utf-8 -*-
"""
text_utils.py / whisper_utils.py の純粋関数に対するユニットテスト。
torch 等の重い依存が無くても実行できる（`python -m unittest discover -s tests`）。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from text_utils import sanitize_filename
from whisper_utils import format_timestamp


class TestSanitizeFilename(unittest.TestCase):
    def test_removes_invalid_chars(self):
        self.assertEqual(sanitize_filename('a/b\\c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_strips_whitespace(self):
        self.assertEqual(sanitize_filename("  name  "), "name")

    def test_empty_becomes_unknown(self):
        self.assertEqual(sanitize_filename(""), "unknown")
        self.assertEqual(sanitize_filename("   "), "unknown")

    def test_normal_name_unchanged(self):
        self.assertEqual(sanitize_filename("田中太郎"), "田中太郎")


class TestFormatTimestamp(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_timestamp(0), "00:00:00")

    def test_minutes_and_seconds(self):
        self.assertEqual(format_timestamp(125), "00:02:05")

    def test_hours(self):
        self.assertEqual(format_timestamp(3661), "01:01:01")


if __name__ == "__main__":
    unittest.main()
