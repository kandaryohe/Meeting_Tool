import sys
import os

try:
    from dotenv import load_dotenv
    load_dotenv()  # 同じフォルダの .env を読み込む（HF_TOKEN など）
except Exception:
    pass  # python-dotenv が無くても環境変数が直接設定されていれば動く

from whisper_utils import load_whisper_pipeline, transcribe_file, transcribe_segments, format_timestamp
import diarize_utils

# 処理対象とする拡張子のリスト
SUPPORTED_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.mp4', '.flac'}


def _build_labeled_transcript(whisper_pipe, diarization_pipe, file_path):
    """
    1本の音声ファイル（Meet等の録音のように全員の声が混ざっている想定）を
    文字起こし＋話者分離し、「[時刻] 話者N: 発言」形式のテキストを返す。
    """
    diar_segments = diarize_utils.diarize(diarization_pipe, file_path)

    lines = []
    for start, end, text in transcribe_segments(whisper_pipe, file_path):
        speaker = diarize_utils.assign_speaker(diar_segments, start, end)
        lines.append(f"[{format_timestamp(start)}] {speaker}: {text}")
    return "\n".join(lines)


if __name__ == "__main__":
    # ディレクトリの設定
    input_dir = "input"
    output_dir = "output"

    # 入力ディレクトリの確認
    if not os.path.exists(input_dir):
        print(f"エラー: '{input_dir}' ディレクトリが見つかりません。作成して音声ファイルを入れてください。")
        sys.exit(1)

    # 出力ディレクトリの作成（なければ作る）
    if not os.path.exists(output_dir):
        os.makedirs(output_dir)
        print(f"'{output_dir}' ディレクトリを作成しました。")

    # 処理対象ファイルのリストアップ
    target_files = []
    for filename in os.listdir(input_dir):
        base, ext = os.path.splitext(filename)
        if ext.lower() in SUPPORTED_EXTENSIONS:
            target_files.append(os.path.join(input_dir, filename))

    target_files.sort()  # ファイル名順にソート

    if not target_files:
        print(f"警告: '{input_dir}' 内に処理対象の音声ファイルが見つかりませんでした。")
        print(f"対応形式: {SUPPORTED_EXTENSIONS}")
        sys.exit(0)

    print(f"合計 {len(target_files)} 件のファイルを処理します。")

    use_diarization = diarize_utils.is_available()
    if use_diarization:
        print("★ 話者分離(speaker diarization)が有効です。発言ごとに話者を推定して書き起こします。")
    else:
        print("話者分離は無効です（HF_TOKEN未設定、または pyannote.audio 未導入）。")
        print("Google Meet等、1本の音声に複数人の声が混ざっている場合は README を参照して有効化してください。")
    print("-" * 30)

    try:
        # 1. モデルロード (ループの外で1回だけ実行)
        pipe = load_whisper_pipeline()
        diarization_pipe = diarize_utils.load_diarization_pipeline() if use_diarization else None
        print("-" * 30)

        # 2. 各ファイルを順次処理
        for i, input_path in enumerate(target_files, 1):
            filename = os.path.basename(input_path)

            try:
                # 文字起こし実行
                if diarization_pipe is not None:
                    text = _build_labeled_transcript(pipe, diarization_pipe, input_path)
                else:
                    text = transcribe_file(pipe, input_path)

                # 出力ファイル名の生成 (input/sample.mp3 -> output/sample.txt)
                file_base_name = os.path.splitext(filename)[0]
                output_filename = f"{file_base_name}.txt"
                output_path = os.path.join(output_dir, output_filename)

                # ファイル保存
                with open(output_path, "w", encoding="utf-8") as f:
                    f.write(text)

                print(f"[{i}/{len(target_files)}] 完了: {output_filename} に保存しました。")

            except Exception as e:
                print(f"[{i}/{len(target_files)}] エラー ({filename}): {e}")
                continue  # 次のファイルの処理へ進む

        print("\n" + "=" * 30)
        print("すべての処理が完了しました。")
        print("=" * 30)

    except Exception as e:
        print(f"\n予期せぬエラーが発生しました: {e}")
