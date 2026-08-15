# -*- coding: utf-8 -*-
"""
Discord議事録bot 自動起動ウォッチャー（画面なし常駐）

- Discord が起動していたら discord_bot.py を自動起動
- Discord が終了したら bot を自動停止
- pythonw.exe で実行することで、画面に何も出さずに裏で動く。
  Windowsログイン時にスタートアップから自動起動される。
"""

import os
import sys
import time
import subprocess
from datetime import datetime

BASE = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(BASE, ".venv", "Scripts", "python.exe")
BOT = os.path.join(BASE, "discord_bot.py")
OUT_LOG = os.path.join(BASE, "bot.out.log")
ERR_LOG = os.path.join(BASE, "bot.err.log")
WATCHER_LOG = os.path.join(BASE, "watcher.log")

CREATE_NO_WINDOW = 0x08000000  # 起動する bot に黒い窓を出さないフラグ


def notify(title, message):
    """画面にポップアップ（メッセージボックス）を出す。見張り役をブロックしないよう別プロセスで表示。"""
    # 0x40=情報アイコン, 0x1000=最前面表示, 0x10000=最前面ウィンドウとして表示
    code = (
        "import ctypes;"
        f"ctypes.windll.user32.MessageBoxW(0, {message!r}, {title!r}, 0x40 | 0x1000)"
    )
    try:
        subprocess.Popen([PYTHON, "-c", code], creationflags=CREATE_NO_WINDOW)
    except Exception as e:
        wlog(f"通知の表示に失敗: {e}")


def wlog(msg):
    ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    try:
        with open(WATCHER_LOG, "a", encoding="utf-8") as f:
            f.write(f"{ts} {msg}\n")
    except Exception:
        pass


def discord_running():
    """このPCで Discord.exe が起動しているか。"""
    try:
        out = subprocess.run(
            ["tasklist", "/FI", "IMAGENAME eq Discord.exe", "/NH"],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        ).stdout
        return "Discord.exe" in out
    except Exception:
        return False


def get_bot_pids():
    """discord_bot.py を実行中の python プロセスのPID一覧を返す（重複起動防止）。"""
    ps_cmd = (
        "Get-CimInstance Win32_Process -Filter \"Name='python.exe'\" | "
        "Where-Object { $_.CommandLine -like '*discord_bot.py*' } | "
        "Select-Object -ExpandProperty ProcessId"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-Command", ps_cmd],
            capture_output=True, text=True, creationflags=CREATE_NO_WINDOW,
        ).stdout
        return [int(x) for x in out.split() if x.strip().isdigit()]
    except Exception:
        return []


def start_bot():
    try:
        out_f = open(OUT_LOG, "a", encoding="utf-8")
        err_f = open(ERR_LOG, "a", encoding="utf-8")
        subprocess.Popen(
            [PYTHON, "-u", BOT],
            cwd=BASE, stdout=out_f, stderr=err_f,
            creationflags=CREATE_NO_WINDOW,
        )
        notify("議事録bot", "🎙️ 議事録botが起動しました。\n録音開始ボタン（または /record）が使えます。")
    except Exception as e:
        wlog(f"bot起動に失敗: {e}")


def stop_bots(pids):
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                           creationflags=CREATE_NO_WINDOW,
                           capture_output=True)
        except Exception as e:
            wlog(f"bot停止に失敗(PID {pid}): {e}")


def main():
    wlog("ウォッチャー開始")

    while True:
        d = discord_running()
        pids = get_bot_pids()

        if d and not pids:
            wlog("Discord検知 → bot を起動します")
            start_bot()
        elif (not d) and pids:
            wlog("Discord終了 → bot を停止します")
            stop_bots(pids)
            notify("議事録bot", "⏹️ 議事録botが終了しました。")

        time.sleep(7)


if __name__ == "__main__":
    main()
