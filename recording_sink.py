# -*- coding: utf-8 -*-
"""py-cord 2.8.1 の音声受信APIに対応した独自Sink。

なぜ自作が必要か
----------------
py-cord 2.7 で音声受信の仕組みが作り直されたが、同梱の `discord.sinks.WaveSink`
は新しい仕組みが要求する以下を実装しておらず、`start_recording` が即座に
AttributeError で落ちる（本家 issue #3139 / 音声受信は作業中）。

  - `__sink_listeners__` … イベント登録に使われる
  - `walk_children()`    … 子Sinkの列挙に使われる
  - `is_opus()`          … デコードの要否判定に使われる

さらに旧APIとの差分が2つある。

  - `sink.init(vc)` が呼ばれなくなったので `sink.vc` は呼び出し側で設定する
  - `write()` に渡るのが「PCMバイト列 + ユーザーID」ではなく
    `VoiceData` オブジェクト + `Member` オブジェクトに変わった

一方で音声を受け取る経路自体は生きているため、不足分を補ったSinkを用意すれば録音できる。
なお `start_recording` は `isinstance(sink, Sink)` を検査するので Sink の継承は必須。

出力形式: 48kHz / 2ch / 16bit （Opusデコーダの出力そのまま）
"""

from __future__ import annotations

import threading
import wave
from collections import defaultdict
from pathlib import Path

import discord

# Opusデコーダの出力仕様（discord.opus.Decoder と一致させる）
SAMPLE_RATE = 48000
CHANNELS = 2
SAMPLE_WIDTH = 2  # 16bit


class MeetingSink(discord.sinks.Sink):
    """話者ごとにPCMを溜め込み、WAVとして書き出すSink。"""

    # 新APIが参照する属性。イベントは使わないので空にしておく。
    __sink_listeners__ = ()

    def __init__(self) -> None:
        super().__init__()
        # user_id -> PCMバイト列
        self._pcm: dict[int, bytearray] = defaultdict(bytearray)
        self._lock = threading.Lock()

    # -- 新APIが要求するもの -------------------------------------------------

    def is_opus(self) -> bool:
        """Falseを返すとデコード済みPCMが渡ってくる。"""
        return False

    def walk_children(self):
        """子Sinkは持たない。"""
        return ()

    # -- 音声の受け取り ------------------------------------------------------

    def write(self, data, user) -> None:  # data: VoiceData, user: Member|User|Object|None
        """パケット1つ分のPCMを話者ごとに追記する。受信スレッドから呼ばれる。"""
        pcm = getattr(data, "pcm", None)
        if not pcm:
            return

        user_id = getattr(user, "id", None)
        if user_id is None:
            return

        with self._lock:
            self._pcm[user_id].extend(pcm)

    def cleanup(self) -> None:
        self.finished = True

    # -- 書き出し ------------------------------------------------------------

    def has_audio(self) -> bool:
        with self._lock:
            return any(len(buf) > 0 for buf in self._pcm.values())

    def user_ids(self) -> list[int]:
        with self._lock:
            return [uid for uid, buf in self._pcm.items() if len(buf) > 0]

    def write_wav(self, user_id: int, path: Path) -> int:
        """1人分のPCMをWAVとして保存し、書き込んだバイト数を返す。"""
        with self._lock:
            pcm = bytes(self._pcm.get(user_id, b""))

        if not pcm:
            return 0

        with wave.open(str(path), "wb") as wf:
            wf.setnchannels(CHANNELS)
            wf.setsampwidth(SAMPLE_WIDTH)
            wf.setframerate(SAMPLE_RATE)
            wf.writeframes(pcm)
        return len(pcm)

    def duration_seconds(self, user_id: int) -> float:
        with self._lock:
            n = len(self._pcm.get(user_id, b""))
        return n / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH)
