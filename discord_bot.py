# -*- coding: utf-8 -*-
"""
Discord 議事録作成bot

やること:
  1. ボタン（または /record）でボイスチャンネルの録音を開始（話者ごとに別トラック）
  2. 停止した瞬間に音声を recordings/<会議名>/ に自動保存
  3. そのまま文字起こし→議事録作成のパイプラインを自動実行
  4. できあがった議事録を Discord のテキストチャンネルに投稿

⚠️ 会議は必ず「ステージチャンネル」で行うこと。
   Discord は 2026-03-02 に DAVE(E2E暗号化) を必須化したため、通常の
   ボイスチャンネルには DAVE 非対応の bot は接続できない（4017）。
   ステージチャンネルはこの必須化の対象外なので、そこでなら録音できる。

操作方法:
  /panel  … 「録音開始」「停止して議事録作成」ボタン付きのパネルを設置する（推奨）
  /record … 録音開始（ボタンと同じ）
  /stop   … 停止＆議事録作成（ボタンと同じ）
  /status … 現在の録音状態を確認
  /leave  … 状態がおかしくなったときの強制退出（復旧用）

パネルは一度設置すればbotを再起動しても押せます（永続View）。

必要なライブラリ:
  py-cord 2.7.2。上げても下げても録音できない。
  詳細は requirements-bot.txt のコメントを参照。

起動:
  .venv（Python 3.13）で実行してください。通常はウォッチャーが自動起動します。
"""

import os
import sys
import wave
import struct
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

# 録音トラックがこのバイト数未満なら「実質無音」とみなす（WAVヘッダのみ等）
MIN_TRACK_BYTES = 8000

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


async def _wait_connected(vc: discord.VoiceClient, timeout: float = 15.0) -> bool:
    """音声接続の確立を待つ。

    connect() から戻った直後はまだハンドシェイクが終わっておらず、
    そのまま start_recording すると「Not connected to voice channel」で落ちる。
    """
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if vc.is_connected():
            return True
        await asyncio.sleep(0.5)
    return vc.is_connected()


async def _force_disconnect(vc) -> None:
    """例外を無視して確実にボイスチャンネルから抜ける。"""
    if vc is None:
        return
    try:
        await vc.disconnect(force=True)
    except Exception as e:
        print(f"切断時のエラー(無視): {e}")


def _extract_pcm(raw: bytes):
    """py-cord が返すバイト列から PCM 本体と音声形式を取り出す。

    py-cord の WaveSink.format_audio は wave.open() でヘッダを書くだけで
    writeframes を呼ばないため、data チャンクのサイズが 0 の壊れた WAV に
    なる（RIFFサイズも36のまま）。ffmpeg は寛容なので読めてしまうが、
    Python の wave モジュールは「0秒」と判定する。
    そこでヘッダを捨てて PCM だけを取り出し、正しい WAV として書き直す。

    返り値: (PCMバイト列, チャンネル数, サンプル幅バイト, サンプリングレート)
    """
    channels, width, rate = 2, 2, 48000  # Opusデコーダの既定値

    if raw[:4] == b"RIFF":
        i = raw.find(b"fmt ")
        if i != -1 and len(raw) >= i + 24:
            try:
                channels = struct.unpack("<H", raw[i + 10:i + 12])[0] or channels
                rate = struct.unpack("<I", raw[i + 12:i + 16])[0] or rate
                bits = struct.unpack("<H", raw[i + 22:i + 24])[0] or 16
                width = bits // 8
            except struct.error:
                pass
        j = raw.find(b"data")
        if j != -1:
            return raw[j + 8:], channels, width, rate

    return raw, channels, width, rate


def _explain_voice_error(e: Exception) -> str:
    """ボイス接続の失敗コードに、原因と対処の説明を付ける。

    Discord は 2026-03-02 に DAVE(E2E暗号化) を必須化した。
    DAVE 非対応のクライアントは通常のボイスチャンネルに入れない。
    ただし「ステージチャンネル」はこの必須化の対象外なので、
    そちらを使えば録音できる。
    """
    msg = str(e)
    if "4017" in msg:
        return (
            "\n\n**原因**: このチャンネルは DAVE(E2E暗号化) 対応を必須にしています。"
            "Discordが2026年3月から通常のボイスチャンネルで必須化したもので、"
            "botのライブラリ(py-cord)がまだ対応できていません。\n"
            "**対処**: 会議を **ステージチャンネル** で行ってください。"
            "ステージチャンネルはこの必須化の対象外です。"
        )
    if "4006" in msg:
        return (
            "\n\n**原因**: ボイスゲートウェイのセッションが無効です。"
            "py-cord のバージョンが古すぎる可能性があります（2.7.2 が必要）。"
        )
    if "4014" in msg:
        return "\n\n**原因**: botにこのチャンネルへの接続権限がありません。"
    return ""


# ----------------------------------------------------------------------
# 録音の開始・停止（スラッシュコマンドとボタンの共通処理）
# ----------------------------------------------------------------------
async def start_recording_flow(guild, member, text_channel) -> str:
    """録音を開始し、ユーザーに返すメッセージを文字列で返す。"""
    if guild.id in connections:
        return "すでに録音中です。停止するには停止ボタン（または /stop）を使ってください。"

    if not member.voice or not member.voice.channel:
        return "先にボイスチャンネルに参加してから開始してください。"

    channel = member.voice.channel

    # 前回の接続が残っていると新しい接続を確立できないので、掃除してから入り直す。
    # （botを強制終了した後などに発生する）
    if guild.voice_client is not None:
        await _force_disconnect(guild.voice_client)
        await asyncio.sleep(1)

    try:
        vc = await channel.connect(timeout=30.0, reconnect=False)
    except Exception as e:
        return f"ボイスチャンネルへの接続に失敗しました: {e}{_explain_voice_error(e)}"

    if not await _wait_connected(vc):
        await _force_disconnect(vc)
        return (
            "ボイスチャンネルには入れましたが、音声接続が確立できませんでした。\n"
            "もう一度お試しください。繰り返す場合はネットワークがDiscordの音声通信を"
            "ブロックしている可能性があります。"
        )

    meeting_name = datetime.datetime.now().strftime("%Y-%m-%d_%H%M%S")
    meeting_dir = REC_DIR / meeting_name
    meeting_dir.mkdir(parents=True, exist_ok=True)

    connections[guild.id] = {"vc": vc, "dir": meeting_dir, "channel": text_channel}

    # 録音開始。停止すると on_recording_finished が呼ばれる。
    try:
        vc.start_recording(
            discord.sinks.WaveSink(),   # 話者ごとに WAV を作る
            on_recording_finished,      # 停止時に呼ばれるコールバック
            guild.id,                   # コールバックへ渡す追加引数
        )
    except Exception as e:
        connections.pop(guild.id, None)
        await _force_disconnect(vc)
        return f"録音の開始に失敗しました: {e}"

    print(f"録音開始: {meeting_name} / チャンネル={channel.name}")
    return (
        f"🎙️ 録音を開始しました（**{meeting_name}**）。\n"
        f"チャンネル: **{channel.name}**\n"
        f"終了するには **停止ボタン**（または /stop）を押してください。"
    )


async def stop_recording_flow(guild) -> str:
    """録音を停止し、ユーザーに返すメッセージを文字列で返す。"""
    if guild.id not in connections:
        return "現在は録音していません。"

    vc = connections[guild.id]["vc"]
    try:
        vc.stop_recording()  # -> on_recording_finished が呼ばれる
    except Exception as e:
        # ここで応答せずに落ちると「アプリケーションが応答しませんでした」になるため必ず返す
        connections.pop(guild.id, None)
        await _force_disconnect(vc)
        return (
            f"録音の停止に失敗しました: {e}\n"
            "接続を切りました。もう一度開始からやり直してください。"
        )

    return "⏹️ 録音を停止しました。文字起こし→議事録作成を始めます…（数分かかります）"


# ----------------------------------------------------------------------
# 操作パネル（ボタン）
# ----------------------------------------------------------------------
class RecordPanel(discord.ui.View):
    """録音開始・停止のボタンを持つパネル。

    timeout=None かつ custom_id 付きなので、bot を再起動しても
    設置済みのパネルのボタンがそのまま使える（永続View）。
    """

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="録音開始", emoji="🔴",
        style=discord.ButtonStyle.danger, custom_id="minutes_bot_start",
    )
    async def start_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        # ボイス接続に3秒以上かかることがあるため、先に応答を保留する
        await interaction.response.defer()
        msg = await start_recording_flow(
            interaction.guild, interaction.user, interaction.channel
        )
        await interaction.followup.send(msg)

    @discord.ui.button(
        label="停止して議事録作成", emoji="⏹️",
        style=discord.ButtonStyle.primary, custom_id="minutes_bot_stop",
    )
    async def stop_button(self, button: discord.ui.Button, interaction: discord.Interaction):
        await interaction.response.defer()
        msg = await stop_recording_flow(interaction.guild)
        await interaction.followup.send(msg)


@bot.event
async def on_ready():
    # 設置済みパネルのボタンを再起動後も有効にする
    bot.add_view(RecordPanel())
    print("=" * 50)
    print(f"ログイン成功: {bot.user}  (bot準備完了)")
    print("Discordで /panel を実行すると、録音開始・停止ボタンを設置できます。")
    print("※ 会議は「ステージチャンネル」で行ってください（通常のVCは4017で接続不可）。")
    print("=" * 50)
    _cleanup_old_recordings()


@bot.slash_command(description="録音の開始・停止ボタンを設置します")
async def panel(ctx: discord.ApplicationContext):
    await ctx.respond(
        "**🎙️ 議事録作成bot**\n"
        "1. **ステージチャンネル**に参加し「ステージを開始」でスピーカーになる\n"
        "2. **録音開始** を押す\n"
        "3. 会議が終わったら **停止して議事録作成** を押す\n"
        "→ 文字起こしと議事録がこのチャンネルに投稿されます。",
        view=RecordPanel(),
    )


# ----------------------------------------------------------------------
# スラッシュコマンド（ボタンと同じ処理）
# ----------------------------------------------------------------------
@bot.slash_command(description="今いるボイスチャンネルの録音を開始します")
async def record(ctx: discord.ApplicationContext):
    await ctx.defer()
    msg = await start_recording_flow(ctx.guild, ctx.author, ctx.channel)
    await ctx.respond(msg)


@bot.slash_command(description="録音を停止し、議事録の作成を開始します")
async def stop(ctx: discord.ApplicationContext):
    await ctx.defer()
    msg = await stop_recording_flow(ctx.guild)
    await ctx.respond(msg)


@bot.slash_command(description="botをボイスチャンネルから強制的に退出させます（復旧用）")
async def leave(ctx: discord.ApplicationContext):
    """録音状態がおかしくなったときの復旧用。録音は破棄される。"""
    info = connections.pop(ctx.guild.id, None)
    vc = ctx.guild.voice_client

    if info is None and vc is None:
        await ctx.respond("botはボイスチャンネルにいません。", ephemeral=True)
        return

    await _force_disconnect(vc)
    await ctx.respond("👋 ボイスチャンネルから退出しました（録音中だった場合は破棄されます）。", ephemeral=True)


@bot.slash_command(description="現在の録音状態を確認します")
async def status(ctx: discord.ApplicationContext):
    if ctx.guild.id in connections:
        name = connections[ctx.guild.id]["dir"].name
        await ctx.respond(f"🔴 録音中です（{name}）。停止するには停止ボタン。", ephemeral=True)
    else:
        await ctx.respond("⚪ 現在は録音していません。録音開始ボタンで始められます。", ephemeral=True)


# ----------------------------------------------------------------------
# 録音停止後の処理（保存 → 文字起こし → 議事録 → 投稿）
# ----------------------------------------------------------------------
async def on_recording_finished(sink: discord.sinks.WaveSink, guild_id: int):
    """録音停止時に py-cord から呼ばれる。音声を保存し、議事録パイプラインを実行する。"""
    info = connections.pop(guild_id, None)

    # ボイスチャンネルから退出
    await _force_disconnect(getattr(sink, "vc", None))

    if info is None:
        return
    meeting_dir: Path = info["dir"]
    channel = info["channel"]
    guild = bot.get_guild(guild_id)

    # 各話者の音声を保存
    saved = 0
    skipped_silent = 0
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
            pcm, channels, width, rate = _extract_pcm(audio.file.read())

            # 中身が無いトラックを渡すと文字起こしが幻聴を起こすので捨てる
            if len(pcm) < MIN_TRACK_BYTES:
                print(f"スキップ（ほぼ無音）: {display}  {len(pcm)}バイト")
                skipped_silent += 1
                continue

            # py-cord の壊れたヘッダを使わず、正しい WAV として書き直す
            with wave.open(str(out_path), "wb") as wf:
                wf.setnchannels(channels)
                wf.setsampwidth(width)
                wf.setframerate(rate)
                wf.writeframes(pcm)

            secs = len(pcm) / (rate * channels * width)
            print(f"保存: {out_path.name}  ({secs:.1f}秒 / {len(pcm):,}バイト)")
            saved += 1
        except Exception as e:
            print(f"音声保存エラー ({display}): {e}")

    if saved == 0:
        await channel.send(
            "⚠️ 録音データが空でした。\n"
            "ステージチャンネルで**「ステージを開始」してスピーカーになっているか**"
            "確認してください（聴衆のままだと音声が流れません）。\n"
            f"（受信トラック数: {len(sink.audio_data)} / 無音として破棄: {skipped_silent}）"
        )
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
