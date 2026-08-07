# -*- coding: utf-8 -*-
"""
話者分離（speaker diarization）ヘルパー。

Discordモードは話者ごとに音声ファイルが分かれているため不要だが、
Google Meet等を手元で録音した「1本の音声に全員の声が混ざっている」
ケース（手動モード）では、これを使って発言ごとに話者を推定する。

pyannote.audio は関数内で遅延importするため、このモジュール自体は
重い依存なしで読み込める（テスト用）。
"""

import os

MODEL_ID = "pyannote/speaker-diarization-3.1"


def is_available() -> bool:
    """話者分離が使える環境かどうか（HF_TOKEN設定 & pyannote.audio導入済み）。"""
    if not os.environ.get("HF_TOKEN", "").strip():
        return False
    try:
        import pyannote.audio  # noqa: F401
    except ImportError:
        return False
    return True


def load_diarization_pipeline():
    """pyannote の話者分離パイプラインを初期化して返す（重いので1回だけ呼ぶ）。"""
    from pyannote.audio import Pipeline
    import torch

    hf_token = os.environ.get("HF_TOKEN", "").strip()
    if not hf_token:
        raise RuntimeError(
            "HF_TOKEN が未設定です。話者分離には Hugging Face のアクセストークンが必要です。"
            "https://huggingface.co/pyannote/speaker-diarization-3.1 で利用規約に同意し、"
            "https://huggingface.co/settings/tokens で発行したトークンを .env に設定してください。"
        )

    pipeline = Pipeline.from_pretrained(MODEL_ID, use_auth_token=hf_token)
    if torch.cuda.is_available():
        pipeline.to(torch.device("cuda"))
    return pipeline


def diarize(pipeline, file_path):
    """
    音声ファイルを話者分離し、(start_sec, end_sec, speaker_label) のリストを
    開始時刻順に返す。speaker_label は「話者1」「話者2」...のように
    出現順の連番へ正規化する。
    """
    result = pipeline(file_path)

    label_map = {}
    segments = []
    for turn, _, raw_speaker in result.itertracks(yield_label=True):
        if raw_speaker not in label_map:
            label_map[raw_speaker] = f"話者{len(label_map) + 1}"
        segments.append((turn.start, turn.end, label_map[raw_speaker]))

    segments.sort(key=lambda x: x[0])
    return segments


def assign_speaker(diarization_segments, start, end):
    """
    Whisperの1発言セグメント (start, end) に、時間的に最も重なりが大きい
    話者ラベルを割り当てる。重なりが無ければ中心時刻が最も近いセグメントの
    話者を採用する。diarization_segments が空なら "不明" を返す。
    """
    if not diarization_segments:
        return "不明"

    best_label = None
    best_overlap = 0.0
    for d_start, d_end, label in diarization_segments:
        overlap = min(end, d_end) - max(start, d_start)
        if overlap > best_overlap:
            best_overlap = overlap
            best_label = label

    if best_label is not None:
        return best_label

    mid = (start + end) / 2
    nearest_label = None
    nearest_dist = None
    for d_start, d_end, label in diarization_segments:
        center = (d_start + d_end) / 2
        dist = abs(center - mid)
        if nearest_dist is None or dist < nearest_dist:
            nearest_dist = dist
            nearest_label = label

    return nearest_label
