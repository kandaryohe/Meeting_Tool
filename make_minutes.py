# -*- coding: utf-8 -*-
"""音声フォルダから議事録を作る（議事録作成.bat から呼ばれる）。

案内文をすべてここに置いてあるのは、バッチファイルに日本語を書くと
コマンドプロンプトの文字コード設定によって解析が壊れるため。
Python はコンソールの設定に関係なく日本語を正しく出力できる。

使い方:
    py -3.14 make_minutes.py <音声フォルダ>
"""

import subprocess
import sys
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
PIPELINE = BASE_DIR / "pipeline.py"
SUPPORTED = {".wav", ".mp3", ".m4a", ".mp4", ".flac"}

LINE = "=" * 44


def wait_and_exit(code: int) -> None:
    print()
    try:
        input("Enterキーを押すと閉じます...")
    except EOFError:
        pass
    sys.exit(code)


def ask_for_folder() -> str:
    print("音声フォルダが指定されていません。")
    print()
    print("【使い方】")
    print("  録音した音声が入ったフォルダを、議事録作成.bat に")
    print("  ドラッグ＆ドロップしてください。")
    print()
    print("  Craig で録音した場合は、multi-track をダウンロードして")
    print("  展開したフォルダをドロップします。")
    print()
    try:
        return input("フォルダのパスを貼り付けても実行できます: ").strip().strip('"')
    except EOFError:
        return ""


def main() -> None:
    print(LINE)
    print("  議事録作成")
    print(LINE)
    print()

    target = sys.argv[1].strip().strip('"') if len(sys.argv) > 1 else ""
    if not target:
        target = ask_for_folder()
    if not target:
        print()
        print("[中止] フォルダが指定されませんでした。")
        wait_and_exit(1)

    folder = Path(target)
    if not folder.is_dir():
        print()
        print("[エラー] フォルダが見つかりません:")
        print(f"  {target}")
        print("  ※ファイルではなく「フォルダ」を指定してください。")
        wait_and_exit(1)

    audio = [p for p in sorted(folder.iterdir()) if p.suffix.lower() in SUPPORTED]
    if not audio:
        print()
        print("[エラー] 音声ファイルが見つかりません:")
        print(f"  {folder}")
        print(f"  対応形式: {' / '.join(sorted(e.lstrip('.') for e in SUPPORTED))}")
        wait_and_exit(1)

    if not PIPELINE.exists():
        print(f"[エラー] pipeline.py が見つかりません: {PIPELINE}")
        wait_and_exit(1)

    print(f"対象フォルダ: {folder}")
    print(f"音声ファイル: {len(audio)}件")
    for p in audio:
        print(f"  - {p.name}")
    print()
    print("文字起こし→議事録作成を開始します。")
    print("CPUで処理するため、会議が長いほど時間がかかります。")
    print("このウィンドウは閉じないでください。")
    print()

    result = subprocess.run(
        [sys.executable, str(PIPELINE), str(folder)],
        cwd=str(BASE_DIR),
    )

    print()
    if result.returncode != 0:
        print(LINE)
        print("[エラー] 処理に失敗しました。")
        print("  ・上に表示されたエラー内容を確認してください")
        print("  ・要約されない場合は .env の GEMINI_API_KEY を確認してください")
        print(LINE)
        wait_and_exit(result.returncode)

    print(LINE)
    print("完了しました！")
    print(f"{folder} の中に")
    print("  〇〇_書き起こし.txt … 誰がいつ何を言ったか")
    print("  〇〇_議事録.txt     … 要約された議事録")
    print("が出来ています。")
    print(LINE)
    wait_and_exit(0)


if __name__ == "__main__":
    main()
