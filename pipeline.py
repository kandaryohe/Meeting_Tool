# -*- coding: utf-8 -*-
"""
議事録作成パイプライン（Discord録音bot用）

役割:
  1. 1つの会議フォルダ（話者ごとの .wav が入っている）を受け取り、
  2. 各トラックを文字起こし（タイムスタンプ付き）し、
  3. 全員の発言を時刻順に並べて「誰がいつ何を言ったか」の書き起こしを作り、
  4. Gemini で議事録に要約する。

使い方（単体実行）:
  py -3.14 pipeline.py <会議フォルダのパス>
  例: py -3.14 pipeline.py "recordings/2026-07-28_1530"

このスクリプトは torch / transformers / google-generativeai が入った
Python環境（このプロジェクトでは 3.14）で動かす想定です。
Discord bot（.venv / 3.13）からは subprocess 経由で呼び出されます。
"""

import os
import sys
import glob
import shutil

try:
    from dotenv import load_dotenv
    load_dotenv()  # 同じフォルダの .env を読み込む（GEMINI_API_KEY など）
except Exception:
    pass  # python-dotenv が無くても環境変数が直接設定されていれば動く


from whisper_utils import load_whisper_pipeline, transcribe_segments, format_timestamp
from gemini_utils import generate_with_retry, make_client, describe_error


def _ensure_ffmpeg_on_path():
    """
    transformers の音声読み込みは ffmpeg を使う。
    PATH に無い場合、環境変数 FFMPEG_PATH や WinGet のインストール先を探して
    実行時に PATH へ追加する（シェル再起動なしで動くようにするための保険）。
    """
    if shutil.which("ffmpeg"):
        return True

    candidates = []
    env_path = os.environ.get("FFMPEG_PATH", "").strip()
    if env_path:
        candidates.append(env_path)

    # WinGet (Gyan.FFmpeg) の標準的なインストール先を探す
    local = os.environ.get("LOCALAPPDATA", "")
    if local:
        pattern = os.path.join(
            local, "Microsoft", "WinGet", "Packages",
            "Gyan.FFmpeg*", "**", "bin", "ffmpeg.exe",
        )
        candidates.extend(glob.glob(pattern, recursive=True))

    for exe in candidates:
        if exe and os.path.exists(exe):
            os.environ["PATH"] = os.path.dirname(exe) + os.pathsep + os.environ.get("PATH", "")
            print(f"ffmpeg を検出し PATH に追加しました: {exe}")
            return True

    print("警告: ffmpeg が見つかりません。音声の読み込みに失敗する可能性があります。")
    return False

# 対応する音声拡張子
SUPPORTED_EXTENSIONS = {".wav", ".mp3", ".m4a", ".mp4", ".flac"}

# 要約に使う Gemini モデル。
# バージョンを固定すると提供終了時に 404 で議事録が作れなくなるため
# （実際に gemini-2.5-pro が "no longer available" になった）、
# 最新版を追従するエイリアスを使う。
#
# gemini-pro-latest は無料枠だとクォータ超過(429)になるため flash を既定にする。
# 品質を上げたい場合は .env に GEMINI_MODEL=gemini-pro-latest を設定する（有料枠が必要）。
GEMINI_MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")


def build_transcript(meeting_dir):
    """
    会議フォルダ内の全音声トラックを文字起こしし、
    時刻順に「[時刻] 話者: 発言」形式でまとめた文字列を返す。
    """
    audio_files = [
        f for f in sorted(glob.glob(os.path.join(meeting_dir, "*")))
        if os.path.splitext(f)[1].lower() in SUPPORTED_EXTENSIONS
    ]

    if not audio_files:
        raise FileNotFoundError(
            f"'{meeting_dir}' に音声ファイル({', '.join(sorted(SUPPORTED_EXTENSIONS))})が見つかりません。"
        )

    print(f"合計 {len(audio_files)} トラックを処理します。")
    _ensure_ffmpeg_on_path()
    pipe = load_whisper_pipeline()

    all_segments = []  # (start, end, speaker, text)
    for i, path in enumerate(audio_files, 1):
        # ファイル名（拡張子なし）を話者名として使う
        speaker = os.path.splitext(os.path.basename(path))[0]
        print(f"[{i}/{len(audio_files)}] 文字起こし中: {speaker} ...")
        try:
            for start, end, text in transcribe_segments(pipe, path):
                all_segments.append((start, end, speaker, text))
        except Exception as e:
            print(f"  -> 警告: {speaker} のトラックでエラー: {e}")

    # 発言開始時刻でソート（＝会話の流れの順番に並ぶ）
    all_segments.sort(key=lambda x: x[0])

    lines = [f"[{format_timestamp(start)}] {speaker}: {text}"
             for start, end, speaker, text in all_segments]
    return "\n".join(lines)


# ======================================================================
#  要約（Gemini）
# ======================================================================
def summarize_with_gemini(transcript_text):
    """書き起こしテキストを Gemini で議事録に要約する。失敗時は None を返す。"""
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key or api_key.startswith("ここに"):
        print("警告: GEMINI_API_KEY が未設定のため、要約はスキップします（書き起こしのみ出力）。")
        return None

    try:
        client = make_client(api_key)
    except Exception as e:
        print(f"警告: Gemini クライアントを作れないため要約をスキップします: {e}")
        return None

    prompt_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "prompt.txt")
    system_instruction = None
    if os.path.exists(prompt_path):
        with open(prompt_path, "r", encoding="utf-8") as f:
            system_instruction = f.read().strip()

    print(f"★ Gemini ({GEMINI_MODEL_NAME}) で議事録を作成中...")
    try:
        return generate_with_retry(
            client, transcript_text, GEMINI_MODEL_NAME,
            system_instruction=system_instruction,
        )
    except Exception as e:
        print(f"警告: 要約に失敗しました（リトライ済み）: {describe_error(e)}")
        print("　書き起こしは保存済みです。同じフォルダで再実行すれば要約だけやり直せます。")
        return None


# ======================================================================
#  メイン処理
# ======================================================================
def run(meeting_dir):
    """
    会議フォルダを処理し、書き起こしと議事録を同フォルダ内に保存する。
    返り値: 議事録ファイルのパス（要約できなかった場合は書き起こしのパス）。

    途中で中断された場合に備えて、既にできている成果物は作り直さない。
    文字起こしは録音時間の2倍以上かかることがあるため、
    書き起こしが残っていればそれを再利用して要約だけやり直す。
    """
    meeting_dir = os.path.abspath(meeting_dir)
    meeting_name = os.path.basename(meeting_dir.rstrip("/\\"))

    transcript_path = os.path.join(meeting_dir, f"{meeting_name}_書き起こし.txt")
    minutes_path = os.path.join(meeting_dir, f"{meeting_name}_議事録.txt")

    # 既に議事録まで出来ていれば何もしない
    if os.path.exists(minutes_path) and os.path.getsize(minutes_path) > 0:
        print(f"既に議事録があります。処理をスキップします: {minutes_path}")
        return minutes_path

    # 1. 書き起こし（既にあれば再利用する＝中断からの再開）
    if os.path.exists(transcript_path) and os.path.getsize(transcript_path) > 0:
        with open(transcript_path, "r", encoding="utf-8") as f:
            transcript = f.read()
        print(f"既存の書き起こしを再利用します（文字起こしはやり直しません）: {transcript_path}")
    else:
        transcript = build_transcript(meeting_dir)
        with open(transcript_path, "w", encoding="utf-8") as f:
            f.write(transcript)
        print(f"✓ 書き起こしを保存: {transcript_path}")

    if not transcript.strip():
        print("発言が検出されませんでした。議事録は作成しません。")
        return transcript_path

    # 2. 要約（議事録化）
    summary = summarize_with_gemini(transcript)
    if summary is None:
        return transcript_path

    with open(minutes_path, "w", encoding="utf-8") as f:
        f.write(summary)
    print(f"✓ 議事録を保存: {minutes_path}")
    return minutes_path


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("使い方: python pipeline.py <会議フォルダのパス>")
        sys.exit(1)

    target = sys.argv[1]
    if not os.path.isdir(target):
        print(f"エラー: フォルダが見つかりません: {target}")
        sys.exit(1)

    try:
        result_path = run(target)
        # bot 側がパスを拾えるよう、最終行に目印付きで出力
        print(f"RESULT_PATH::{result_path}")
    except Exception as e:
        print(f"エラー: 処理に失敗しました: {e}")
        sys.exit(1)
