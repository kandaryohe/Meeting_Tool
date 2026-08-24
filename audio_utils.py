# -*- coding: utf-8 -*-
"""録音データ（PCM / WAV）の共通処理。

discord_bot.py（.venv / Python 3.13）から使うが、
discord などの重い依存を持たないのでテストからも直接読み込める。
"""

import struct
import wave

# Opus デコーダの既定値。fmt チャンクを読めなかったときはこれを使う。
DEFAULT_CHANNELS = 2
DEFAULT_WIDTH = 2       # 16bit = 2バイト
DEFAULT_RATE = 48000


def extract_pcm(raw: bytes):
    """py-cord が返すバイト列から PCM 本体と音声形式を取り出す。

    py-cord の WaveSink.format_audio は wave.open() でヘッダを書くだけで
    writeframes を呼ばないため、data チャンクのサイズが 0 の壊れた WAV に
    なる（RIFFサイズも36のまま）。ffmpeg は寛容なので読めてしまうが、
    Python の wave モジュールは「0秒」と判定する。
    そこでヘッダを捨てて PCM だけを取り出し、正しい WAV として書き直す。

    返り値: (PCMバイト列, チャンネル数, サンプル幅バイト, サンプリングレート)
    """
    channels, width, rate = DEFAULT_CHANNELS, DEFAULT_WIDTH, DEFAULT_RATE

    if raw[:4] == b"RIFF":
        i = raw.find(b"fmt ")
        if i != -1 and len(raw) >= i + 24:
            try:
                channels = struct.unpack("<H", raw[i + 10:i + 12])[0] or channels
                rate = struct.unpack("<I", raw[i + 12:i + 16])[0] or rate
                bits = struct.unpack("<H", raw[i + 22:i + 24])[0] or 16
                width = bits // 8
            except struct.error:
                pass
        j = raw.find(b"data")
        if j != -1:
            return raw[j + 8:], channels, width, rate

    return raw, channels, width, rate


def write_wav(path, pcm: bytes, channels: int, width: int, rate: int) -> None:
    """PCM を正しいヘッダ付きの WAV として書き出す。"""
    with wave.open(str(path), "wb") as wf:
        wf.setnchannels(channels)
        wf.setsampwidth(width)
        wf.setframerate(rate)
        wf.writeframes(pcm)


def pcm_duration_seconds(pcm: bytes, channels: int, width: int, rate: int) -> float:
    """PCM の長さを秒で返す。形式が壊れている場合は 0 を返す。"""
    bytes_per_second = rate * channels * width
    if bytes_per_second <= 0:
        return 0.0
    return len(pcm) / bytes_per_second
