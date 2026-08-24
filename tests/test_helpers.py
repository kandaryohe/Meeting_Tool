# -*- coding: utf-8 -*-
"""
text_utils.py / whisper_utils.py / audio_utils.py の純粋関数に対するユニットテスト。
torch や py-cord が無くても実行できる（`python -m unittest discover -s tests`）。
"""

import os
import struct
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from text_utils import sanitize_filename
from whisper_utils import format_timestamp
from audio_utils import extract_pcm, pcm_duration_seconds
from gemini_utils import generate_with_retry, is_capacity_error


def _wave_header(channels=2, rate=48000, bits=16, data_size=0):
    """py-cord が作るのと同じ形の WAV ヘッダを組み立てる。

    py-cord は writeframes を呼ばないため data サイズが 0 のまま出てくる。
    その状態を再現できるよう data_size を指定できるようにしてある。
    """
    byte_rate = rate * channels * bits // 8
    block_align = channels * bits // 8
    return (
        b"RIFF" + struct.pack("<I", 36 + data_size) + b"WAVE"
        + b"fmt " + struct.pack("<IHHIIHH", 16, 1, channels, rate, byte_rate, block_align, bits)
        + b"data" + struct.pack("<I", data_size)
    )


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


class TestExtractPcm(unittest.TestCase):
    def test_broken_pycord_header_is_stripped(self):
        """dataサイズ0の壊れたヘッダでも、PCM本体を取り出せる。"""
        pcm = b"\x01\x02" * 100
        raw = _wave_header(data_size=0) + pcm
        self.assertEqual(extract_pcm(raw), (pcm, 2, 2, 48000))

    def test_reads_format_from_header(self):
        pcm = b"\x00" * 64
        raw = _wave_header(channels=1, rate=16000, bits=8) + pcm
        self.assertEqual(extract_pcm(raw), (pcm, 1, 1, 16000))

    def test_raw_pcm_without_header_uses_defaults(self):
        """ヘッダが無いバイト列はそのまま PCM として扱う。"""
        pcm = b"\x7f" * 32
        self.assertEqual(extract_pcm(pcm), (pcm, 2, 2, 48000))

    def test_truncated_header_falls_back_to_defaults(self):
        """fmt チャンクが途中で切れていても既定値で処理を続ける。"""
        raw = b"RIFF" + b"\x00" * 4 + b"WAVEfmt "
        self.assertEqual(extract_pcm(raw), (raw, 2, 2, 48000))

    def test_empty_input(self):
        self.assertEqual(extract_pcm(b""), (b"", 2, 2, 48000))


class TestPcmDuration(unittest.TestCase):
    def test_one_second(self):
        # 48000Hz / 2ch / 16bit = 1秒あたり 192000 バイト
        self.assertAlmostEqual(pcm_duration_seconds(b"\x00" * 192000, 2, 2, 48000), 1.0)

    def test_broken_format_returns_zero(self):
        """0除算で落ちないこと（形式を読めなかった場合の保険）。"""
        self.assertEqual(pcm_duration_seconds(b"\x00" * 100, 0, 0, 0), 0.0)


class _FakeResponse:
    def __init__(self, text):
        self.text = text


class _FakeModels:
    """モデルごとに「成功させるか、例外を投げるか」を決められる偽の client.models。"""

    def __init__(self, behavior):
        self.behavior = behavior      # {モデル名: 例外 or 返す文字列}
        self.calls = []               # 呼ばれたモデル名の記録

    def generate_content(self, model, contents, config=None):
        self.calls.append(model)
        result = self.behavior[model]
        if isinstance(result, Exception):
            raise result
        return _FakeResponse(result)


class _FakeClient:
    def __init__(self, behavior):
        self.models = _FakeModels(behavior)


BUSY = Exception("503 UNAVAILABLE. This model is currently experiencing high demand.")
QUOTA = Exception("429 RESOURCE_EXHAUSTED. quota exceeded")
BAD_KEY = Exception("400 INVALID_ARGUMENT. API key not valid")


class TestIsCapacityError(unittest.TestCase):
    def test_busy_and_quota_are_capacity_errors(self):
        self.assertTrue(is_capacity_error(BUSY))
        self.assertTrue(is_capacity_error(QUOTA))

    def test_other_errors_are_not(self):
        self.assertFalse(is_capacity_error(BAD_KEY))
        self.assertFalse(is_capacity_error(Exception("404 NOT_FOUND")))


class TestFallbackModel(unittest.TestCase):
    """本命モデルが混雑したら予備モデルに切り替わること。"""

    def _run(self, behavior, **kwargs):
        client = _FakeClient(behavior)
        # max_retries=1 にしてリトライ待ちを発生させない（テストを速く保つ）
        text = generate_with_retry(
            client, "書き起こし", "main-model", max_retries=1, **kwargs
        )
        return text, client.models.calls

    def test_uses_primary_when_it_works(self):
        text, calls = self._run(
            {"main-model": "議事録"}, fallback_model="spare-model"
        )
        self.assertEqual(text, "議事録")
        self.assertEqual(calls, ["main-model"])

    def test_switches_to_fallback_when_busy(self):
        text, calls = self._run(
            {"main-model": BUSY, "spare-model": "議事録"}, fallback_model="spare-model"
        )
        self.assertEqual(text, "議事録")
        self.assertEqual(calls, ["main-model", "spare-model"])

    def test_does_not_switch_on_non_capacity_error(self):
        """キー不正など、別モデルにしても直らないエラーでは切り替えない。"""
        with self.assertRaises(Exception) as cm:
            self._run(
                {"main-model": BAD_KEY, "spare-model": "議事録"},
                fallback_model="spare-model",
            )
        self.assertIn("API key not valid", str(cm.exception))

    def test_no_fallback_configured(self):
        with self.assertRaises(Exception) as cm:
            self._run({"main-model": BUSY})
        self.assertIn("503", str(cm.exception))

    def test_same_model_is_not_retried_as_fallback(self):
        client = _FakeClient({"main-model": BUSY})
        with self.assertRaises(Exception):
            generate_with_retry(
                client, "書き起こし", "main-model",
                max_retries=1, fallback_model="main-model",
            )
        self.assertEqual(client.models.calls, ["main-model"])

    def test_raises_when_fallback_also_fails(self):
        with self.assertRaises(Exception) as cm:
            self._run(
                {"main-model": BUSY, "spare-model": QUOTA}, fallback_model="spare-model"
            )
        self.assertIn("429", str(cm.exception))


if __name__ == "__main__":
    unittest.main()
