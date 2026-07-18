import os
import sys
import glob
import google.generativeai as genai

# ======================================================================
# 【設定】使用するAIモデルの指定
# ======================================================================
# 用途に合わせて以下のモデル名を変更してください。
#
# ▼ 提供されている主なテキスト生成モデル
# - "gemini-2.5-flash" : 標準モデル。高速かつ低コストで、日常的なタスクや長文の要約に最適です。
# - "gemini-2.5-pro"   : 上位モデル。より高度な推論、複雑な指示の理解、繊細な文章の作成が必要な場合に使用します。
# - "gemini-1.5-flash" : 1世代前の高速モデル。
# - "gemini-1.5-pro"   : 1世代前の上位モデル。
#
MODEL_NAME = "gemini-2.5-pro"
# ======================================================================

def setup_gemini():
    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        print("エラー: 環境変数 'GEMINI_API_KEY' が設定されていません。")
        sys.exit(1)

    genai.configure(api_key=api_key)

    prompt_path = "prompt.txt"
    if not os.path.exists(prompt_path):
        print(f"エラー: プロンプトファイル '{prompt_path}' が見つかりません。")
        sys.exit(1)

    with open(prompt_path, "r", encoding="utf-8") as f:
        system_instruction = f.read().strip()

    # Geminiモデルの初期化 (上部で設定した MODEL_NAME を読み込みます)
    model = genai.GenerativeModel(
        model_name=MODEL_NAME,
        system_instruction=system_instruction
    )
    return model

if __name__ == "__main__":
    output_dir = "output"

    if not os.path.exists(output_dir):
        print(f"エラー: '{output_dir}' ディレクトリが見つかりません。先に文字起こしを実行してください。")
        sys.exit(1)

    print(f"★ Gemini API ({MODEL_NAME}) による要約処理を開始します...")
    gemini_model = setup_gemini()

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
            response = gemini_model.generate_content(transcribed_text)
            with open(summary_path, "w", encoding="utf-8") as f:
                f.write(response.text)
            print(f"  -> ✓ 保存完了: {os.path.basename(summary_path)}")
        except Exception as e:
            print(f"  -> エラーが発生しました: {e}")

    print("要約処理がすべて完了しました。")
