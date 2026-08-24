# -*- coding: utf-8 -*-
"""
Whisper（kotoba-whisper）文字起こしの共通ヘルパー。

pipeline.py から使われる。torch / transformers は関数内で遅延importするため、
このモジュール自体は重い依存なしで読み込める（テスト用）。
"""

MODEL_ID = "kotoba-tech/kotoba-whisper-v2.2"


def load_whisper_pipeline():
    """モデルとパイプラインを初期化して返す（重いので1回だけ呼ぶ）。"""
    import torch
    from transformers import pipeline

    device = "cpu"
    torch_dtype = torch.float32

    if torch.cuda.is_available():
        device = "cuda:0"
        torch_dtype = torch.float16
        print("★ NVIDIA GPU (CUDA) を使用します")
    elif getattr(torch.backends, "mps", None) is not None and torch.backends.mps.is_available():
        device = "mps"
        torch_dtype = torch.float16
        print("★ Apple Silicon GPU (MPS) を使用します")
    else:
        print("★ CPU を使用します（処理に時間がかかります）")

    print(f"モデル {MODEL_ID} をロード中...（初回はダウンロードがあり時間がかかります）")

    return pipeline(
        "automatic-speech-recognition",
        model=MODEL_ID,
        torch_dtype=torch_dtype,
        device=device,
        chunk_length_s=30,
        batch_size=8,
    )


def transcribe_segments(pipe, file_path):
    """
    1つの音声ファイルを文字起こしし、タイムスタンプ付きセグメントの一覧を返す。
    返り値: [(start_sec, end_sec, text), ...]
    """
    result = pipe(file_path, return_timestamps=True)

    segments = []
    for chunk in result.get("chunks", []):
        text = (chunk.get("text") or "").strip()
        if not text:
            continue
        ts = chunk.get("timestamp") or (None, None)
        start = ts[0] if ts[0] is not None else 0.0
        end = ts[1] if len(ts) > 1 and ts[1] is not None else start
        segments.append((float(start), float(end), text))

    # chunks が空でも text があれば1セグメントとして扱う（保険）
    if not segments:
        text = (result.get("text") or "").strip()
        if text:
            segments.append((0.0, 0.0, text))

    return segments


def format_timestamp(seconds) -> str:
    """秒 -> [HH:MM:SS] 形式の文字列。"""
    seconds = int(seconds)
    h = seconds // 3600
    m = (seconds % 3600) // 60
    s = seconds % 60
    return f"{h:02d}:{m:02d}:{s:02d}"
