import torch
from transformers import pipeline
import sys
import os
import glob

# 処理対象とする拡張子のリスト
SUPPORTED_EXTENSIONS = {'.mp3', '.wav', '.m4a', '.mp4', '.flac'}

def load_whisper_pipeline():
    """
    モデルとパイプラインを初期化して返します。
    ループの外で一度だけ実行するために分離しました。
    """
    model_id = "kotoba-tech/kotoba-whisper-v2.2"
    
    # デバイスの自動判定
    device = "cpu"
    torch_dtype = torch.float32

    if torch.cuda.is_available():
        device = "cuda:0"
        torch_dtype = torch.float16
        print("★ NVIDIA GPU (CUDA) を使用します")
    elif torch.backends.mps.is_available():
        device = "mps"
        torch_dtype = torch.float16
        print("★ Apple Silicon GPU (MPS) を使用します")
    else:
        print("★ CPU を使用します（処理に時間がかかります）")

    print(f"モデル {model_id} をロード中... (これには少し時間がかかります)")

    # パイプラインの構築
    pipe = pipeline(
        "automatic-speech-recognition",
        model=model_id,
        torch_dtype=torch_dtype,
        device=device,
        chunk_length_s=30,
        batch_size=8,
    )
    
    return pipe

def transcribe_file(pipe, file_path):
    """
    ロード済みのパイプラインを使って個別のファイルを文字起こしします。
    """
    print(f"処理中: {os.path.basename(file_path)} ...")
    
    # return_timestamps=True でタイムスタンプ情報も扱えます
    # generate_kwargs={"language": "japanese"} を明示する場合もありますが、kotoba-whisperは自動でうまく扱います
    result = pipe(file_path, return_timestamps=True)
    
    return result["text"]

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
    
    target_files.sort() # ファイル名順にソート

    if not target_files:
        print(f"警告: '{input_dir}' 内に処理対象の音声ファイルが見つかりませんでした。")
        print(f"対応形式: {SUPPORTED_EXTENSIONS}")
        sys.exit(0)

    print(f"合計 {len(target_files)} 件のファイルを処理します。")
    print("-" * 30)

    try:
        # 1. モデルロード (ループの外で1回だけ実行)
        pipe = load_whisper_pipeline()
        print("-" * 30)

        # 2. 各ファイルを順次処理
        for i, input_path in enumerate(target_files, 1):
            filename = os.path.basename(input_path)
            
            try:
                # 文字起こし実行
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
                continue # 次のファイルの処理へ進む

        print("\n" + "="*30)
        print("すべての処理が完了しました。")
        print("="*30)

    except Exception as e:
        print(f"\n予期せぬエラーが発生しました: {e}")