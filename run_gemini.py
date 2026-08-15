import os
import sys
import glob

try:
    from dotenv import load_dotenv
    load_dotenv()  # 同じフォルダの .env を読み込む（GEMINI_API_KEY など）
except Exception:
    pass  # python-dotenv が無くても環境変数が直接設定されていれば動く

from gemini_utils import generate_with_retry, make_client

# ======================================================================
# 【設定】使用するAIモデルの指定
# ======================================================================
# バージョンを固定すると提供終了時に 404 で要約できなくなるため
# （実際に gemini-2.5-pro が "no longer available" になった）、
# 最新版を追従するエイリアスを使う。
#
# gemini-pro-latest は無料枠だとクォータ超過(429)になるため flash を既定にする。
# 品質を上げたい場合は環境変数 GEMINI_MODEL=gemini-pro-latest を設定する（有料枠が必要）。
MODEL_NAME = os.environ.get("GEMINI_MODEL", "gemini-flash-latest")
# ======================================================================

def setup_gemini():
    """(クライアント, システム指示) を返す。"""
    try:
        client = make_client()
    except Exception as e:
        print(f"エラー: {e}")
        sys.exit(1)

    prompt_path = "prompt.txt"
    if not os.path.exists(prompt_path):
        print(f"エラー: プロンプトファイル '{prompt_path}' が見つかりません。")
        sys.exit(1)

    with open(prompt_path, "r", encoding="utf-8") as f:
        system_instruction = f.read().strip()

    return client, system_instruction

if __name__ == "__main__":
    output_dir = "output"

    if not os.path.exists(output_dir):
        print(f"エラー: '{output_dir}' ディレクトリが見つかりません。先に文字起こしを実行してください。")
        sys.exit(1)

    print(f"★ Gemini API ({MODEL_NAME}) による要約処理を開始します...")
    gemini_client, system_instruction = setup_gemini()

    # 要約対象のファイルを取得（すでに要約済みの _summary.txt は除外）
    txt_files = [f for f in glob.glob(os.path.join(output_dir, "*.txt")) if not f.endswith("_summary.txt")]

    if not txt_files:
        print(f"警告: '{output_dir}' 内に要約対象のテキストファイルが見つかりません。")
        sys.exit(0)

    for i, file_path in enumerate(txt_files, 1):
        filename = os.path.basename(file_path)
        file_base_name = os.path.splitext(filename)[0]
        summary_path = os.path.join(output_dir, f"{file_base_name}_summary.txt")

        print(f"[{i}/{len(txt_files)}] {filename} を要約中...")

        with open(file_path, "r", encoding="utf-8") as f:
            transcribed_text = f.read()

        if not transcribed_text.strip():
            print(f"  -> テキストが空のためスキップします。")
            continue

        try:
            summary = generate_with_retry(
                gemini_client, transcribed_text, MODEL_NAME,
                system_instruction=system_instruction,
            )
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(summary)
            print(f"  -> ✓ 保存完了: {os.path.basename(summary_path)}")
        except Exception as e:
            print(f"  -> エラーが発生しました（リトライ済み）: {e}")

    print("要約処理がすべて完了しました。")
