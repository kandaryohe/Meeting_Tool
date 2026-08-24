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
import json
import asyncio
import datetime
import shutil
import subprocess
from pathlib import Path

import discord
from dotenv import load_dotenv

from text_utils import sanitize_filename
from audio_utils import extract_pcm, write_wav, pcm_duration_seconds


# ----------------------------------------------------------------------
# ログ出力（時刻付き）
# ----------------------------------------------------------------------
class _TimestampedStream:
    """書き込まれた各行の先頭に時刻を付ける出力ストリーム。

    bot はウォッチャーから起動され、出力は bot.out.log にリダイレクトされる。
    時刻が入っていないと「いつ落ちたのか」「何回起動したのか」を
    後から追えないため、py-cord 自身が出す行も含めてまとめて時刻を付ける。
    """

    def __init__(self, stream):
        self._stream = stream
        self._need_prefix = True

    def write(self, text):
        if not text:
            return 0
        stamp = datetime.datetime.now().strftime("[%Y-%m-%d %H:%M:%S] ")
        out = []
        for part in text.splitlines(keepends=True):
            if self._need_prefix:
                out.append(stamp)
            out.append(part)
            self._need_prefix = part.endswith(("\n", "\r"))
        return self._stream.write("".join(out))

    def flush(self):
        self._stream.flush()

    def __getattr__(self, name):
        # __init__ で _stream を設定済みなので、ここに来るのは他の属性だけ
        return getattr(object.__getattribute__(self, "_stream"), name)


sys.stdout = _TimestampedStream(sys.stdout)
sys.stderr = _TimestampedStream(sys.stderr)


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

# 文字起こし～議事録作成を実行中の会議フォルダ（str）。
# /status の表示と、同じ会議を二重に処理しないための管理に使う。
processing: set = set()

# 起動時の掃除・再開を一度だけ実行するための目印（on_ready は再接続でも呼ばれる）
_startup_done = False

# 会議フォルダに残すメタ情報のファイル名。
# bot が落ちたあと、どのチャンネルへ結果を返せばよいか復元するために使う。
META_NAME = "_meeting.json"

# 「発言が検出されなかった会議」に pipeline.py が置く目印。
# これがある会議は何度やり直しても結果が変わらないので再開の対象から外す。
# pipeline.py の NO_SPEECH_MARKER と同じ名前にすること。
NO_SPEECH_MARKER = "_no_speech.txt"

# pipeline.py が返す処理結果（pipeline.py の STATUS_* と対応）
STATUS_OK = "ok"
STATUS_NO_SPEECH = "no_speech"
STATUS_SUMMARY_FAILED = "summary_failed"

# 音声ファイルとして扱う拡張子（未処理判定に使う）
AUDIO_EXTS = (".wav", ".mp3", ".m4a", ".mp4", ".flac")


def _write_meta(meeting_dir: Path, guild_id: int, channel_id: int) -> None:
    """結果の投稿先を会議フォルダに記録する（再起動後の再開用）。"""
    try:
        (meeting_dir / META_NAME).write_text(
            json.dumps(
                {
                    "guild_id": guild_id,
                    "channel_id": channel_id,
                    "started_at": datetime.datetime.now().isoformat(timespec="seconds"),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
    except Exception as e:
        print(f"メタ情報の保存に失敗 ({meeting_dir.name}): {e}")


def _read_meta(meeting_dir: Path):
    try:
        return json.loads((meeting_dir / META_NAME).read_text(encoding="utf-8"))
    except Exception:
        return None


def _cleanup_old_recordings():
    """不要になった録音フォルダを削除する（容量対策）。

    - RECORDINGS_RETENTION_DAYS より古いもの
    - 中身が空のまま残ったもの（接続に失敗した回などで出来る）

    空フォルダは1時間の猶予を置いてから消す。録音開始直後は
    メタ情報を書くまでの一瞬だけ空になるため、その巻き添えを避ける。
    """
    now = datetime.datetime.now().timestamp()
    cutoff = now - RECORDINGS_RETENTION_DAYS * 86400
    empty_cutoff = now - 3600

    for entry in REC_DIR.iterdir():
        if not entry.is_dir():
            continue
        try:
            mtime = entry.stat().st_mtime
            if RECORDINGS_RETENTION_DAYS > 0 and mtime < cutoff:
                shutil.rmtree(entry)
                print(f"古い録音フォルダを削除しました（{RECORDINGS_RETENTION_DAYS}日超過）: {entry.name}")
            elif not any(entry.iterdir()) and mtime < empty_cutoff:
                entry.rmdir()
                print(f"空の録音フォルダを削除しました: {entry.name}")
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
async def _ensure_stage_instance(channel: discord.StageChannel) -> str | None:
    """ステージが立っていなければ開始する。失敗したら理由の文言を返す。

    ステージが開催中でないと、誰もスピーカーになれない（＝音声が流れない）。
    """
    instance = channel.instance
    if instance is None:
        try:
            instance = await channel.fetch_instance()
        except Exception:
            instance = None
    if instance is not None:
        return None

    try:
        topic = f"会議 {datetime.datetime.now().strftime('%m/%d %H:%M')}"
        await channel.create_instance(topic=topic)
        print(f"ステージを自動開始しました: {topic}")
        return None
    except Exception as e:
        return (
            f"ステージの自動開始に失敗しました（{e}）。"
            "手動で「ステージを開始」を押してください。"
        )


@bot.event
async def on_voice_state_update(member: discord.Member, before, after):
    """ステージチャンネルに入った人を自動でスピーカーに昇格させる。

    ステージは後から入った人が必ず「聴衆」になる仕様で、
    そのままだと発言できず録音にも乗らない。毎回モデレーターが
    招待するのは現実的でないため、参加を検知して自動で昇格させる。
    """
    if member.bot:
        return

    channel = after.channel
    if not isinstance(channel, discord.StageChannel):
        return

    # 入室・移動・聴衆化のいずれでもないなら何もしない
    if before.channel == after.channel and before.suppress == after.suppress:
        return

    if not after.suppress:
        return  # すでにスピーカー

    # ステージが立っていないと誰もスピーカーになれない
    note = await _ensure_stage_instance(channel)
    if note:
        print(note)

    # 「スピーカーになりたい」を押した人は即座に承認する。
    # 本人が押した時点で同意は取れているので、承認待ちにする意味がない。
    if after.requested_to_speak_at is not None:
        try:
            await member.edit(suppress=False)
            print(f"発言リクエストを自動承認: {member.display_name}")
        except Exception as e:
            print(f"発言リクエストの承認に失敗 ({member.display_name}): {e}")
        return

    # ここから先は「スピーカーへの招待」を出す。
    # Discord は他人のマイクを勝手にオンにすることを許さないため、
    # bot にできるのは招待までで、最後の「追加」は本人が押す必要がある。
    # モデレーター権限を持っていても Discord が自動でスピーカーにすることは
    # ないので、権限の有無にかかわらず全員に招待を出す。
    #
    # 入室直後は音声状態が安定しておらず編集が弾かれることがあるので少し待つ
    await asyncio.sleep(1)

    try:
        await member.edit(suppress=False)
        print(f"スピーカーに自動昇格: {member.display_name}")
    except Exception as e:
        print(f"スピーカー昇格に失敗 ({member.display_name}): {e}")


async def _prepare_stage(channel: discord.StageChannel) -> list[str]:
    """ステージチャンネルを録音できる状態にする。

    ステージチャンネルは「ステージが開始されていて、かつ本人がスピーカー」で
    ないと音声が流れない（聴衆は suppress=True で発言できない）。
    毎回この操作を人手でやるのは面倒なので、bot 側で自動的に

      1. ステージが未開始なら開始する
      2. 参加者を全員スピーカーに昇格させる

    を行う。2 には「メンバーをミュート」権限が必要（招待時に付与済み）。
    失敗しても録音自体は続行し、手動対応を促すメモを返す。
    """
    notes: list[str] = []

    # 1. ステージが立っていなければ開始する
    note = await _ensure_stage_instance(channel)
    if note:
        notes.append(note)

    # 2. 聴衆のままの参加者をスピーカーに昇格させる
    promoted, failed = 0, 0
    for m in channel.members:
        if m.bot or m.voice is None or not m.voice.suppress:
            continue
        try:
            await m.edit(suppress=False)
            promoted += 1
        except Exception as e:
            failed += 1
            print(f"スピーカー昇格に失敗 ({m.display_name}): {e}")
    if promoted:
        print(f"スピーカーに昇格: {promoted}人")
    if failed:
        notes.append(
            f"{failed}人をスピーカーにできませんでした。"
            "その方は自分で「スピーカーになる」を押してください。"
        )

    return notes


async def start_recording_flow(guild, member, text_channel) -> str:
    """録音を開始し、ユーザーに返すメッセージを文字列で返す。"""
    if guild.id in connections:
        return "すでに録音中です。停止するには停止ボタン（または /stop）を使ってください。"

    if not member.voice or not member.voice.channel:
        return "先にボイスチャンネルに参加してから開始してください。"

    channel = member.voice.channel

    # ステージチャンネルなら、開始とスピーカー昇格を自動でやる
    stage_notes: list[str] = []
    if isinstance(channel, discord.StageChannel):
        stage_notes = await _prepare_stage(channel)

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
    # bot が落ちても結果の投稿先を復元できるよう記録しておく
    _write_meta(meeting_dir, guild.id, text_channel.id)

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
    msg = (
        f"🎙️ 録音を開始しました（**{meeting_name}**）。\n"
        f"チャンネル: **{channel.name}**\n"
        f"終了するには **停止ボタン**（または /stop）を押してください。"
    )
    if stage_notes:
        msg += "\n\n⚠️ " + "\n⚠️ ".join(stage_notes)
    return msg


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
    """接続完了時に呼ばれる。

    注意: on_ready はネットワークが切れて再接続するたびに呼ばれる。
    掃除と再開は起動時に一度だけ実行する（毎回やると、要約に失敗した会議を
    再接続のたびに要約し直して Gemini の1日20回の枠を消費してしまう）。
    """
    global _startup_done

    # 設置済みパネルのボタンを再起動後も有効にする
    bot.add_view(RecordPanel())
    print("=" * 50)
    print(f"ログイン成功: {bot.user}  (bot準備完了)")
    print("Discordで /panel を実行すると、録音開始・停止ボタンを設置できます。")
    print("※ 会議は「ステージチャンネル」で行ってください（通常のVCは4017で接続不可）。")
    print("=" * 50)

    if _startup_done:
        print("（再接続のため、掃除と再開の処理は行いません）")
        return
    _startup_done = True

    _cleanup_old_recordings()
    # 前回の中断で議事録が出来ていない会議があれば、続きから自動で仕上げる
    await _resume_unfinished()


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


@bot.slash_command(description="現在の録音・議事録作成の状態を確認します")
async def status(ctx: discord.ApplicationContext):
    lines = []

    if ctx.guild.id in connections:
        name = connections[ctx.guild.id]["dir"].name
        lines.append(f"🔴 **録音中**です（{name}）。停止するには停止ボタン。")
    else:
        lines.append("⚪ 現在は録音していません。録音開始ボタンで始められます。")

    if processing:
        names = ", ".join(sorted(Path(p).name for p in processing))
        lines.append(
            f"⏳ **議事録を作成中**です（{names}）。\n"
            "　この間は bot を再起動しないでください（処理が中断されます）。"
        )

    await ctx.respond("\n".join(lines), ephemeral=True)


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
            pcm, channels, width, rate = extract_pcm(audio.file.read())

            # 中身が無いトラックを渡すと文字起こしが幻聴を起こすので捨てる
            if len(pcm) < MIN_TRACK_BYTES:
                print(f"スキップ（ほぼ無音）: {display}  {len(pcm)}バイト")
                skipped_silent += 1
                continue

            # py-cord の壊れたヘッダを使わず、正しい WAV として書き直す
            write_wav(out_path, pcm, channels, width, rate)

            secs = pcm_duration_seconds(pcm, channels, width, rate)
            print(f"保存: {out_path.name}  ({secs:.1f}秒 / {len(pcm):,}バイト)")
            saved += 1
        except Exception as e:
            print(f"音声保存エラー ({display}): {e}")

    if saved == 0:
        await _send(
            channel,
            "⚠️ 録音データが空でした。\n"
            "ステージチャンネルで**「ステージを開始」してスピーカーになっているか**"
            "確認してください（聴衆のままだと音声が流れません）。\n"
            f"（受信トラック数: {len(sink.audio_data)} / 無音として破棄: {skipped_silent}）"
        )
        return

    # 投稿は _send を通すこと。ここで例外が出ると議事録の作成自体が
    # 始まらないまま終わってしまう（次回起動時の再開まで放置される）。
    await _send(
        channel,
        f"⏺️ 録音を保存しました（{saved}人分）。\n"
        f"文字起こし→議事録作成を実行中です…しばらくお待ちください。\n"
        f"（CPU処理のため、録音時間のおよそ2〜3倍かかります）"
    )

    await _process_and_post(meeting_dir, channel)


async def _process_and_post(meeting_dir: Path, channel) -> None:
    """会議フォルダを処理して結果を投稿する。

    録音直後と、中断からの再開の両方から呼ばれる。
    同じフォルダを二重に処理しないよう processing で管理する。
    """
    key = str(meeting_dir)
    if key in processing:
        print(f"既に処理中のためスキップ: {meeting_dir.name}")
        return
    processing.add(key)
    try:
        # 重い処理（torch）は別スレッドのサブプロセスで実行し、bot本体をブロックしない
        loop = asyncio.get_running_loop()
        returncode, status, result_path, stderr = await loop.run_in_executor(
            None, _run_pipeline, meeting_dir
        )

        # pipeline.py は「議事録は作れなかったが異常終了でもない」場合も
        # 終了コード 0 で返す。成否は status で判断すること。
        if returncode != 0:
            tail = (stderr or "").strip().splitlines()[-5:]
            detail = "\n".join(tail) if tail else "（詳細不明）"
            print(f"議事録の作成に失敗: {meeting_dir.name} / {detail}")
            await _send(
                channel,
                "⚠️ 議事録の作成中にエラーが発生しました。\n"
                f"保存フォルダ: `{meeting_dir}`\n"
                f"エラー概要:\n```\n{detail}\n```",
            )
            return

        if status == STATUS_NO_SPEECH:
            print(f"発言が検出されませんでした: {meeting_dir.name}")
            await _send(
                channel,
                "⚠️ 録音に発言が見つからなかったため、議事録は作成できませんでした。\n"
                "ステージチャンネルで**スピーカーになっていたか**確認してください"
                "（聴衆のままだと音声が流れません）。\n"
                f"録音データは `{meeting_dir}` に残してあります。",
            )
            return

        if status == STATUS_SUMMARY_FAILED:
            print(f"要約に失敗（書き起こしのみ）: {meeting_dir.name}")
            await _send(
                channel,
                "⚠️ 書き起こしはできましたが、**要約に失敗**しました"
                "（Gemini の利用上限や混雑が原因のことが多いです）。\n"
                "書き起こしを添付します。bot を再起動すると、"
                "文字起こしはやり直さずに要約だけ再実行します。",
                path=result_path,
            )
            return

        if not result_path or not os.path.exists(result_path):
            print(f"議事録の作成に失敗（成果物が見つかりません）: {meeting_dir.name}")
            await _send(
                channel,
                "⚠️ 議事録の作成は完了しましたが、ファイルが見つかりませんでした。\n"
                f"保存フォルダ: `{meeting_dir}`",
            )
            return

        await _send(channel, "✅ 議事録が完成しました！", path=result_path)
        if DELETE_AUDIO_AFTER_PROCESSING:
            _delete_audio_files(meeting_dir)
    finally:
        processing.discard(key)


async def _send(channel, text: str, path: str | None = None) -> None:
    """結果をテキストチャンネルに投稿する。

    投稿先が分からない場合（再開時にメタ情報が無いなど）や、
    投稿自体に失敗した場合でもログには必ず残す。
    """
    if channel is None:
        print(f"[投稿先不明] {text}" + (f" / {path}" if path else ""))
        return
    try:
        if path:
            await channel.send(text, file=discord.File(path))
        else:
            await channel.send(text)
    except Exception as e:
        print(f"Discordへの投稿に失敗: {e} / {text}" + (f" / {path}" if path else ""))


async def _resume_unfinished() -> None:
    """議事録が出来ていない会議を検出し、続きから処理する。

    bot が処理中に再起動されると、文字起こしが終わっていても
    議事録が作られないまま放置される（実際に起きた）。
    起動時に拾い直して自動で完了させる。
    pipeline.py 側が既存の書き起こしを再利用するので、
    文字起こしが済んでいれば数十秒で終わる。
    """
    for d in sorted(REC_DIR.iterdir()):
        if not d.is_dir() or str(d) in processing:
            continue

        name = d.name
        if (d / f"{name}_議事録.txt").exists():
            continue  # 完成済み
        if (d / NO_SPEECH_MARKER).exists():
            continue  # 発言が無かった会議。何度やり直しても結果は同じ

        # 空の書き起こしは「無い」のと同じ扱いにする（pipeline.py と揃える）。
        # そうしないと、やり直しても必ず失敗する会議を毎回拾ってしまう。
        transcript = d / f"{name}_書き起こし.txt"
        has_transcript = transcript.exists() and transcript.stat().st_size > 0
        has_audio = any(p.suffix.lower() in AUDIO_EXTS for p in d.iterdir() if p.is_file())
        if not (has_transcript or has_audio):
            continue  # 素材が無いので何もできない

        # メタ情報が壊れていても、他の会議の再開まで巻き添えにしない
        meta = _read_meta(d) or {}
        channel_id = meta.get("channel_id")
        channel = bot.get_channel(channel_id) if channel_id else None

        stage = "書き起こし済み（要約のみ）" if has_transcript else "音声のみ（文字起こしから）"
        print(f"未完了の会議を検出したので再開します: {name} … {stage}")
        await _send(
            channel,
            f"🔁 中断していた会議 **{name}** の議事録作成を再開します（{stage}）。"
        )
        asyncio.create_task(_process_and_post(d, channel))


def _run_pipeline(meeting_dir: Path):
    """pipeline.py をサブプロセスで実行する。

    返り値: (returncode, 処理結果の状態, 結果ファイルパス, stderr)
    """
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
        return 1, None, None, f"パイプライン用の Python が見つかりません: {e}"

    print(proc.stdout)
    if proc.stderr:
        print("[pipeline stderr]", proc.stderr)

    # pipeline.py が出力する RESULT_STATUS:: / RESULT_PATH:: を拾う
    status, result_path = None, None
    for line in (proc.stdout or "").splitlines():
        if line.startswith("RESULT_STATUS::"):
            status = line[len("RESULT_STATUS::"):].strip()
        elif line.startswith("RESULT_PATH::"):
            result_path = line[len("RESULT_PATH::"):].strip()

    # 状態を出力しない古い pipeline.py に備え、ファイル名から推測する
    if status is None and result_path:
        status = STATUS_OK if result_path.endswith("_議事録.txt") else STATUS_SUMMARY_FAILED

    return proc.returncode, status, result_path, proc.stderr


if __name__ == "__main__":
    bot.run(TOKEN)
