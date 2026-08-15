# -*- coding: utf-8 -*-
"""py-cord 2.8.1 の音声受信バグを補正するモンキーパッチ。

対象: discord.opus.PacketDecoder._decode_packet

修正1: DAVE復号とOpusデコードの順序
    本家の実装は
        pcm = decoder.decode(packet.decrypted_data)   # 先にOpusデコード
        pcm = dave.decrypt(user_id, audio, pcm)       # 後からDAVE復号
    という順序になっている。しかしDAVE(E2E暗号)が有効な通話では、
    パケット本体はDAVEで暗号化されたOpusフレームなので、
    先にDAVE復号してからOpusデコードしないと復元できない。
    順序が逆のため Opus に暗号文が渡り "corrupted stream" で失敗する。

修正2: 壊れたパケットで録音全体を巻き添えにしない
    本家では1パケットのデコード失敗が PacketRouter スレッドの例外となり、
    finally で stop_recording() が呼ばれて録音全体が即終了してしまう。
    復号できないパケットは捨てて録音を継続する。

py-cord 側で issue #3139 が解決されたら、このファイルの適用は不要になる。
"""

from __future__ import annotations

import logging

from discord.opus import PacketDecoder

try:
    import davey
    HAS_DAVEY = True
except ImportError:  # pragma: no cover
    davey = None
    HAS_DAVEY = False

_log = logging.getLogger(__name__)

# パケットの処理結果（デバッグ用）
stats = {"ok_decrypted": 0, "ok_raw": 0, "failed": 0}


def _decode_packet(self, packet):
    """DAVE復号 → Opusデコード の正しい順序で処理する。

    DAVEが有効な通話でも、全パケットが暗号化されているとは限らない
    （暗号化に対応していない参加者がいると平文のまま流れてくる）。
    本家は `can_passthrough()` の判定だけで決め打ちしていて取りこぼすため、
    「復号してからデコード」と「そのままデコード」の両方を試して
    成功したほうを採用する。
    """
    user_id = self._cached_id
    dave = self.sink.client._connection.dave_session if self.sink.client else None

    data = getattr(packet, "decrypted_data", None)
    if not data:
        return packet, b""

    # 試す順番: DAVE復号したもの → 生データ
    candidates = []
    if HAS_DAVEY and dave is not None and user_id is not None:
        try:
            decrypted = dave.decrypt(user_id, davey.MediaType.audio, data)
            if decrypted:
                candidates.append(("decrypted", decrypted))
        except Exception as e:
            _log.debug("DAVE復号に失敗 (user=%s): %s", user_id, e)
    candidates.append(("raw", data))

    for kind, candidate in candidates:
        try:
            pcm = self._decoder.decode(candidate, fec=False)
        except Exception:
            continue
        if pcm:
            stats["ok_decrypted" if kind == "decrypted" else "ok_raw"] += 1
            return packet, pcm

    stats["failed"] += 1
    return packet, b""


def apply() -> None:
    """パッチを適用する。import 後に一度だけ呼ぶ。"""
    if getattr(PacketDecoder._decode_packet, "_patched", False):
        return
    _decode_packet._patched = True
    PacketDecoder._decode_packet = _decode_packet
    print("[パッチ] py-cord音声受信の修正を適用しました（DAVE復号順序・パケット耐性）")
