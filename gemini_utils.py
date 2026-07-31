# -*- coding: utf-8 -*-
"""Gemini API 呼び出しの共通ヘルパー（run_gemini.py / pipeline.py で共有）。"""

import time


def generate_with_retry(model, text, max_retries=3, base_delay=2):
    """
    model.generate_content() を呼び出し、レート制限等の一時的なエラーに備えて
    指数バックオフでリトライする。最終的に失敗した場合は例外を送出する。
    """
    last_error = None
    for attempt in range(1, max_retries + 1):
        try:
            return model.generate_content(text)
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                delay = base_delay * (2 ** (attempt - 1))
                print(
                    f"  -> Gemini呼び出しに失敗（{attempt}/{max_retries}回目）: {e}\n"
                    f"     {delay}秒待って再試行します..."
                )
                time.sleep(delay)

    raise last_error
