# -*- coding: utf-8 -*-
"""
Discord 議事録作成bot

やること:
  1. ボイスチャンネルに参加して録音（話者ごとに別トラック）
  2. /stop で停止した瞬間に音声を recordings/<会議名>/ に自動保存
  3. そのまま文字起こし→議事録作成のパイプラインを自動実行
  4. できあがった議事録を Discord のテキストチャンネルに投稿

コマンド（スラッシュコマンド）:
  /record … 自分が今いるボイスチャンネルの録音を開始
  /stop   … 録音を停止し、議事録作成を開始
  /status … 現在の録音状態を確認

起動:
  .venv（Python 3.13）で実行してください。通常は「議事録bot起動.bat」から起動します。
"""

import os
import sys
import asyncio
import datetime
import shutil
import subprocess
from pathlib import Path

import discord
from dotenv import load_dotenv

from text_utils import sanitize_filename

# ----------------------------------------------------------------------
# 設定の読み込み
# ----------------------------------------------------------------------
BASE_DIR = Path(__file__).resolve().parent
load_dotenv(BASE_DIR / ".env")

TOKEN = os.environ.get("DISCORD_TOKEN")
if not TOKEN or TOKEN.startswith("ここに"):
    print("エラー: .env の DISCORD_TOKEN が設定されていません。")
    print("  1) .env.example を .env にコピー")
    print("  2) DISCORD_TOKEN に Discord Developer Portal のトークンを貼り付け")
    sys.exit(1)

# 録音ファイルの保存先（会議ごとにサブフォルダを作る）
REC_DIR = BASE_DIR / "recordings"
REC_DIR.mkdir(exist_ok=True)

# 議事録作成に成功したら、容量削減のため生の音声ファイルを削除するか
_DELETE_AUDIO_ENV = os.environ.get("DELETE_AUDIO_AFTER_PROCESSING", "true").strip().lower()
DELETE_AUDIO_AFTER_PROCESSING = _DELETE_AUDIO_ENV not in ("false", "0", "no")

# この日数より古い録音フォルダは起動時に自動削除する（0以下で無効化）
try:
    RECORDINGS_RETENTION_DAYS = int(os.environ.get("RECORDINGS_RETENTION_DAYS", "30") or "30")
except ValueError:
    RECORDINGS_RETENTION_DAYS = 30

# 文字起こし＋要約パイプライン
PIPELINE_PY = BASE_DIR / "pipeline.py"
# パイプラインを動かす Python。torch/transformers が入った環境を指定する。
# .env で PIPELINE_PYTHON にフルパスを設定可能。未設定なら py -3.14 を使う。
_pipeline_python_env = os.environ.get("PIPELINE_PYTHON", "").strip()
if _pipeline_python_env:
    PIPELINE_CMD_PREFIX = [_pipeline_python_env]
else:
    PIPELINE_CMD_PREFIX = ["py", "-3.14"]

# ----------------------------------------------------------------------
# bot 本体
# ----------------------------------------------------------------------
intents = discord.Intents.default()

# サーバーID(DISCORD_GUILD_ID)を指定すると、そのサーバーにだけ即座にコマンド登録される。
# 未指定だとグローバル登録になり、コマンドが使えるまで最大1時間ほどかかることがある。
_guild_id = os.environ.get("DISCORD_GUILD_ID", "").strip()
_debug_guilds = [int(_guild_id)] if _guild_id.isdigit() else None

bot = discord.Bot(intents=intents, debug_guilds=_debug_guilds)

# guild_id -> {"vc": VoiceClient, "dir": Path, "channel": TextChannel}
connections = {}


def _cleanup_old_recordings():
    """RECORDINGS_RETENTION_DAYS より古い録音フォルダを削除する（容量対策）。"""
    if RECORDINGS_RETENTION_DAYS <= 0:
        return
    cutoff = datetime.datetime.now().timestamp() - RECORDINGS_RETENTION_DAYS * 86400
    for entry in REC_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            if entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry)
                print(f"古い録音フォルダを削除しました（{RECORDINGS_RETENTION_DAYS}日超過）: {entry.name}")
        except Exception as e:
            print(f"録音フォルダの削除に失敗 ({entry.name}): {e}")


def _delete_audio_files(meeting_dir: Path):
    """議事録作成に成功した後、生の音声ファイルを削除する（書き起こし・議事録は残す）。"""
    for pattern in ("*.wav", "*.mp3", "*.m4a", "*.mp4", "*.flac"):
        for f in meeting_dir.glob(pattern):
            try:
                f.unlink()
            except Exception as e:
                print(f"音声ファイルの削除に失敗 ({f.name}): {e}")


@bot.event
async def on_ready():
    print("=" * 50)
    print(f"ログイン成功: {bot.user}  (bot準備完了)")
    print("Discordで /record を実行すると録音を開始します。")
    print("=" * 50)
    _cleanup_old_recordings()


@bot.slash_command(description="今いるボイスチャンネルの録音を開始します")
async def record(ctx: discord.ApplicationContext):
    # ボイスチャンネルに入っているか確認
    if not ctx.author.voice or not ctx.author.voice.channel:
        await ctx.respond("先にボイスチャンネルに参加してから /record を実行してください。", ephemeral=True)
        return

    if ctx.guild.id in connections:
        await ctx.respond("すでに録音中です。停止するには /stop を使ってください。", ephemeral=True)
        return

    channel = ctx.author.voice.channel
    try:
        vc = await channel.connect()
    except Exception as e:
        await ctx.respond(f"ボイスチャンネルへの接続に失敗しました: {e}", ephemeral=True)
        return

    meeting_name = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    meeting_dir = REC_DIR / meeting_name
    meeting_dir.mkdir(parents=True, exist_ok=True)

    connections[ctx.guild.id] = {"vc": vc, "dir": meeting_dir, "channel": ctx.channel}

    # 録音開始。停止すると on_recording_finished が呼ばれる。
    vc.start_recording(
        discord.sinks.WaveSink(),   # 話者ごとに WAV を作る
        on_recording_finished,      # 停止時に呼ばれるコールバック
        ctx.guild.id,               # コールバックへ渡す追加引数
    )

    await ctx.respond(
        f"🎙️ 録音を開始しました（**{meeting_name}**）。\n"
        f"チャンネル: **{channel.name}**\n"
        f"終了するには **/stop** を実行してください。"
    )


@bot.slash_command(description="録音を停止し、議事録の作成を開始します")
async def stop(ctx: discord.ApplicationContext):
    if ctx.guild.id not in connections:
        await ctx.respond("現在は録音していません。", ephemeral=True)
        return

    vc = connections[ctx.guild.id]["vc"]
    vc.stop_recording()  # -> on_recording_finished が呼ばれる
    await ctx.respond("⏹️ 録音を停止しました。文字起こし→議事録作成を始めます…（数分かかります）")


@bot.slash_command(description="現在の録音状態を確認します")
async def status(ctx: discord.ApplicationContext):
    if ctx.guild.id in connections:
        name = connections[ctx.guild.id]["dir"].name
        await ctx.respond(f"🔴 録音中です（{name}）。停止するには /stop。", ephemeral=True)
    else:
        await ctx.respond("⚪ 現在は録音していません。/record で開始できます。", ephemeral=True)


async def on_recording_finished(sink: discord.sinks.WaveSink, guild_id: int):
    """録音停止時に呼ばれる。音声を保存し、議事録パイプラインを実行する。"""
    info = connections.pop(guild_id, None)
    # ボイスチャンネルから退出
    try:
        await sink.vc.disconnect()
    except Exception:
        pass

    if info is None:
        return
    meeting_dir: Path = info["dir"]
    channel = info["channel"]
    guild = bot.get_guild(guild_id)

    # 各話者の音声を保存
    saved = 0
    for user_id, audio in sink.audio_data.items():
        # サーバーニックネーム優先で話者名を決める
        display = str(user_id)
        member = guild.get_member(user_id) if guild else None
        if member is not None:
            display = member.display_name
        else:
            user = bot.get_user(user_id)
            if user is not None:
                display = user.display_name

        out_path = meeting_dir / f"{sanitize_filename(display)}.wav"
        try:
            audio.file.seek(0)
            with open(out_path, "wb") as f:
                f.write(audio.file.read())
            saved += 1
        except Exception as e:
            print(f"音声保存エラー ({display}): {e}")

    if saved == 0:
        await channel.send("⚠️ 録音データが空でした。誰も発言していなかった可能性があります。")
        return

    await channel.send(
        f"⏺️ 録音を保存しました（{saved}人分）。\n"
        f"文字起こし→議事録作成を実行中です…しばらくお待ちください。"
    )

    # 重い処理（torch）は別スレッドのサブプロセスで実行し、bot本体をブロックしない
    loop = asyncio.get_running_loop()
    returncode, result_path, stderr = await loop.run_in_executor(
        None, _run_pipeline, meeting_dir
    )

    if returncode == 0 and result_path and os.path.exists(result_path):
        try:
            await channel.send(
                "✅ 議事録が完成しました！",
                file=discord.File(result_path),
            )
        except Exception as e:
            await channel.send(f"議事録は作成できましたが、投稿に失敗しました: {e}\n保存先: {result_path}")

        if DELETE_AUDIO_AFTER_PROCESSING:
            _delete_audio_files(meeting_dir)
    else:
        tail = (stderr or "").strip().splitlines()[-5:]
        detail = "\n".join(tail) if tail else "（詳細不明）"
        await channel.send(
            "⚠️ 議事録の作成中にエラーが発生しました。\n"
            f"保存フォルダ: `{meeting_dir}`\n"
            f"エラー概要:\n```\n{detail}\n```"
        )


def _run_pipeline(meeting_dir: Path):
    """pipeline.py をサブプロセスで実行し、(returncode, 結果ファイルパス, stderr) を返す。"""
    cmd = PIPELINE_CMD_PREFIX + [str(PIPELINE_PY), str(meeting_dir)]
    print(f"パイプライン実行: {' '.join(cmd)}")
    try:
        proc = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            cwd=str(BASE_DIR),
        )
    except FileNotFoundError as e:
        return 1, None, f"パイプライン用の Python が見つかりません: {e}"

    print(proc.stdout)
    if proc.stderr:
        print("[pipeline stderr]", proc.stderr)

    # pipeline.py が最終行に出力する RESULT_PATH:: を拾う
    result_path = None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT_PATH::"):
            result_path = line[len("RESULT_PATH::"):].strip()

    return proc.returncode, result_path, proc.stderr


if __name__ == "__main__":
    bot.run(TOKEN)
