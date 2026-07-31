# -*- coding: utf-8 -*-
"""汎用テキストユーティリティ（discord_bot.py などから共有）。"""

import re


def sanitize_filename(name: str) -> str:
    """ファイル名に使えない文字を除去する。"""
    name = re.sub(r'[\\/:*?"<>|]', "_", name).strip()
    return name or "unknown"
