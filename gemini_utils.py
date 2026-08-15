# -*- coding: utf-8 -*-
"""Gemini API 呼び出しの共通ヘルパー（run_gemini.py / pipeline.py で共有）。

SDK は新しい `google-genai` を使う。
旧 `google-generativeai` は提供元がサポートを終了しており、
実行のたびに非推奨警告が出るうえ、新しいモデルへの追従も止まっている。
"""

import os
import re
import time

from google import genai
from google.genai import types

# サーバーが返す推奨待機時間を待つ際の上限（秒）
MAX_RETRY_WAIT = 120


def _retry_delay(error, default):
    """Gemini が返す推奨待機時間（秒）を取り出す。

    レート制限(429)や高負荷(503)のとき、API は
    `'retryDelay': '31s'` や `Please retry in 31.4s` の形で
    どれだけ待てばよいかを返してくる。
    これを無視して短い間隔で再試行しても必ず失敗するため、
    指定があればそれに従う。
    """
    s = str(error)
    m = re.search(r"'retryDelay':\s*'(\d+(?:\.\d+)?)s'", s) or \
        re.search(r"retry in (\d+(?:\.\d+)?)s", s)
    if m:
        # 指定ぴったりだとまだ回復していないことがあるので1秒足す
        return min(float(m.group(1)) + 1, MAX_RETRY_WAIT)
    return default


def describe_error(error) -> str:
    """API エラーを、原因と対処が分かる短い日本語にする。"""
    s = str(error)
    if "RESOURCE_EXHAUSTED" in s or "429" in s:
        return (
            "Gemini の利用上限に達しました（無料枠は1日あたりの回数制限があります）。"
            "時間をおいて再実行するか、.env の GEMINI_MODEL で別モデルを指定してください。"
        )
    if "UNAVAILABLE" in s or "503" in s:
        return "Gemini 側が一時的に混雑しています。しばらくおいて再実行してください。"
    if "PERMISSION_DENIED" in s or "API_KEY" in s.upper():
        return ".env の GEMINI_API_KEY が正しいか確認してください。"
    if "NOT_FOUND" in s or "404" in s:
        return (
            "指定したモデルが見つかりません。提供終了の可能性があります。"
            ".env の GEMINI_MODEL を見直してください。"
        )
    return s


def make_client(api_key=None):
    """Gemini クライアントを作る。api_key 省略時は環境変数から読む。"""
    api_key = api_key or os.environ.get("GEMINI_API_KEY")
    if not api_key:
        raise ValueError("GEMINI_API_KEY が設定されていません。")
    return genai.Client(api_key=api_key)


def generate_with_retry(
    client, text, model_name, system_instruction=None,
    max_retries=3, base_delay=2,
):
    """
    Gemini でテキストを生成し、レート制限等の一時的なエラーに備えて
    指数バックオフでリトライする。最終的に失敗した場合は例外を送出する。

    返り値: 生成されたテキスト（str）
    """
    config = None
    if system_instruction:
        config = types.GenerateContentConfig(system_instruction=system_instruction)

    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            response = client.models.generate_content(
                model=model_name, contents=text, config=config,
            )
            return response.text
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                # サーバーが待機時間を指定していればそれに従う（指数バックオフは代替）
                delay = _retry_delay(e, base_delay * (2 ** (attempt - 1)))
                print(
                    f"  -> Gemini呼び出しに失敗（{attempt}/{max_retries}回目）: "
                    f"{describe_error(e)}\n"
                    f"     {delay:.0f}秒待って再試行します..."
                )
                time.sleep(delay)

    raise last_error
