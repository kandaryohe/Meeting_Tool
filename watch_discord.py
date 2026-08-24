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

import psutil

BASE = os.path.dirname(os.path.abspath(__file__))
PYTHON = os.path.join(BASE, ".venv", "Scripts", "python.exe")
BOT = os.path.join(BASE, "discord_bot.py")
OUT_LOG = os.path.join(BASE, "bot.out.log")
ERR_LOG = os.path.join(BASE, "bot.err.log")
WATCHER_LOG = os.path.join(BASE, "watcher.log")

CREATE_NO_WINDOW = 0x08000000  # 起動する bot に黒い窓を出さないフラグ

CHECK_INTERVAL = 7          # Discord の起動状態を見に行く間隔（秒）
LAUNCH_COOLDOWN = 30        # 起動直後、次の起動判定を保留する時間（秒）
MAX_LOG_BYTES = 2_000_000   # これを超えたログは .old.log に退避してから書き直す


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
    for p in psutil.process_iter(["name"]):
        try:
            if (p.info.get("name") or "").lower() == "discord.exe":
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return False


def get_bot_procs():
    """discord_bot.py を実行中の python プロセスを [(pid, 親pid), ...] で返す。"""
    procs = []
    for p in psutil.process_iter(["pid", "ppid", "name", "cmdline"]):
        try:
            name = (p.info.get("name") or "").lower()
            if name not in ("python.exe", "pythonw.exe"):
                continue
            cmdline = p.info.get("cmdline") or []
            if any("discord_bot.py" in arg for arg in cmdline):
                procs.append((p.info["pid"], p.info.get("ppid")))
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
    return procs


def count_bot_instances(procs):
    """実際に動いている bot の個数を数える。

    venv の python.exe は本体を起動し直す「中継スタブ」なので、
    1つの bot が OS 上は［スタブ＋本体］の2プロセスに見える。
    親も bot プロセスであるものを除くと、実際の起動数になる。
    これを数えないと、正常な1個の bot を多重起動と誤判定してしまう。
    """
    pids = {pid for pid, _ in procs}
    return sum(1 for _, ppid in procs if ppid not in pids)


def rotate_log(path):
    """ログが大きくなりすぎたら .old.log に退避する（無限に肥大させない）。"""
    try:
        if os.path.exists(path) and os.path.getsize(path) > MAX_LOG_BYTES:
            old = path.replace(".log", ".old.log")
            os.replace(path, old)
    except Exception as e:
        wlog(f"ログの退避に失敗 ({os.path.basename(path)}): {e}")


def start_bot():
    """bot を起動し、起動できた場合はその PID を返す。"""
    for path in (OUT_LOG, ERR_LOG):
        rotate_log(path)

    try:
        # with で閉じておく。Popen が複製を渡すので子プロセス側は書き続けられる。
        # 閉じないと起動のたびにハンドルが1組ずつ残る。
        with open(OUT_LOG, "a", encoding="utf-8") as out_f, \
             open(ERR_LOG, "a", encoding="utf-8") as err_f:
            proc = subprocess.Popen(
                [PYTHON, "-u", BOT],
                cwd=BASE, stdout=out_f, stderr=err_f,
                creationflags=CREATE_NO_WINDOW,
            )
        wlog(f"bot を起動しました (PID {proc.pid})")
        notify("議事録bot", "🎙️ 議事録botが起動しました。\n録音開始ボタン（または /record）が使えます。")
        return proc.pid
    except Exception as e:
        wlog(f"bot起動に失敗: {e}")
        return None


def stop_bots(pids):
    for pid in pids:
        try:
            psutil.Process(pid).terminate()
        except psutil.NoSuchProcess:
            pass
        except Exception as e:
            wlog(f"bot停止に失敗(PID {pid}): {e}")


def main():
    rotate_log(WATCHER_LOG)
    wlog("ウォッチャー開始")

    last_launch = 0.0   # 直近で bot を起動した時刻（連続起動の抑止に使う）
    known_pids = []     # 前回の巡回で見えていた bot の PID

    while True:
        d = discord_running()
        procs = get_bot_procs()
        pids = [pid for pid, _ in procs]

        # 見えていた bot が消えた＝落ちた。原因調査のため必ず記録する。
        gone = [p for p in known_pids if p not in pids]
        if gone:
            wlog(f"botプロセスが終了していました (PID {', '.join(map(str, gone))})")
        # 多重起動していると1回の操作にボタンが複数回反応する
        instances = count_bot_instances(procs)
        if instances > 1:
            wlog(f"警告: botが{instances}個動いています (PID {', '.join(map(str, pids))})")
        known_pids = pids

        if d and not pids:
            # 起動直後はプロセスがまだ見えないことがある。
            # そのまま次の巡回で起動し直すと bot が多重起動するため、少し待つ。
            if time.time() - last_launch < LAUNCH_COOLDOWN:
                wlog("bot がまだ見えませんが、起動直後のため待機します")
            else:
                wlog("Discord検知 → bot を起動します")
                start_bot()
                last_launch = time.time()
        elif (not d) and pids:
            wlog("Discord終了 → bot を停止します")
            stop_bots(pids)
            known_pids = []
            notify("議事録bot", "⏹️ 議事録botが終了しました。")

        time.sleep(CHECK_INTERVAL)


if __name__ == "__main__":
    main()
