# -*- coding: utf-8 -*-
"""
diarize_utils.assign_speaker の純粋ロジックに対するユニットテスト。
pyannote.audio 等の重い依存が無くても実行できる。
"""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from diarize_utils import assign_speaker


class TestAssignSpeaker(unittest.TestCase):
    def setUp(self):
        self.segments = [
            (0.0, 5.0, "話者1"),
            (5.0, 10.0, "話者2"),
            (12.0, 20.0, "話者1"),
        ]

    def test_picks_speaker_with_largest_overlap(self):
        self.assertEqual(assign_speaker(self.segments, 1.0, 3.0), "話者1")
        self.assertEqual(assign_speaker(self.segments, 6.0, 9.0), "話者2")

    def test_picks_speaker_with_larger_overlap_across_boundary(self):
        # 3.0-5.5 は「話者1」に2.0秒、「話者2」に0.5秒重なる → 重なりが大きい話者1が採用される
        self.assertEqual(assign_speaker(self.segments, 3.0, 5.5), "話者1")
        self.assertEqual(assign_speaker(self.segments, 4.9, 9.0), "話者2")

    def test_falls_back_to_nearest_when_no_overlap(self):
        # 10.0-12.0 の無音区間はどの発言とも重ならない → 中心時刻が近い方を採用
        self.assertEqual(assign_speaker(self.segments, 10.2, 10.4), "話者2")
        self.assertEqual(assign_speaker(self.segments, 11.85, 11.95), "話者1")

    def test_empty_segments_returns_unknown(self):
        self.assertEqual(assign_speaker([], 0.0, 1.0), "不明")


if __name__ == "__main__":
    unittest.main()
