# 議事録作成ツール

Discord の通話を録音し、自動で文字起こし・議事録作成まで行うツールです。

## できること

Discord bot が通話に参加して録音し、**停止した瞬間に自動で**

1. 話者ごとの音声を保存
2. 文字起こし（誰がいつ何を言ったか付き）
3. Gemini で議事録に要約
4. できあがった議事録を Discord に投稿

までを行います。

音声ファイルを手元に持っている場合は、bot を使わず手動で処理することもできます（下記「手動モード」）。
Google Meet などDiscord以外のツールで録音した音声も、手動モードで議事録化できます（話者分離を有効にすれば発言者ごとの区別も可能）。

---

## 構成

| ファイル | 役割 | 実行環境 |
|---|---|---|
| `discord_bot.py` | Discord録音bot本体（録音→保存→パイプライン起動） | `.venv`（Python 3.13） |
| `pipeline.py` | 文字起こし＋議事録作成（bot から呼ばれる） | Python 3.14（torch入り） |
| `run_whisper.py` | 手動：`input/` の音声を文字起こし（話者分離オプション対応） | Python 3.14 |
| `run_gemini.py` | 手動：`output/` のテキストを要約 | Python 3.14 |
| `diarize_utils.py` | 話者分離（pyannote.audio）のヘルパー。1本の音声から発言者を推定する | Python 3.14 |
| `prompt.txt` | 議事録の書式・要約方針を指示するプロンプト | － |
| `議事録bot起動.bat` | bot をワンクリック起動 | － |

> 録音ライブラリ(Pycord)の都合で bot は **Python 3.13**、
> 文字起こし(torch)は既存の **Python 3.14** を使い、bot がサブプロセスで呼び分けます。

---

## セットアップ（初回のみ）

### 1. Discord bot を用意する
1. https://discord.com/developers/applications → **New Application**
2. 左メニュー **Bot** → **Reset Token** でトークンを取得（後で `.env` に貼る）
3. 左メニュー **OAuth2 → URL Generator**
   - SCOPES: `bot` と `applications.commands`
   - BOT PERMISSIONS: `Connect` / `Speak` / `Use Voice Activity` / `Send Messages` / `Attach Files`
   - 生成 URL を開き、**自分のサーバーに招待**

### 2. Gemini API キーを用意する
- https://aistudio.google.com/ で API キーを取得（後で `.env` に貼る）

### 3. `.env` を作る
`.env.example` を `.env` という名前でコピーし、値を設定：
```
DISCORD_TOKEN=（Discordのbotトークン）
GEMINI_API_KEY=（GeminiのAPIキー）
```

> 以下は任意設定です（未設定でも動きます）:
> - `DELETE_AUDIO_AFTER_PROCESSING` … 議事録作成成功後、生の音声ファイルを自動削除するか（既定: `true`）。書き起こし・議事録は残ります。
> - `RECORDINGS_RETENTION_DAYS` … `recordings/` 内の古い会議フォルダを bot 起動時に自動削除するまでの日数（既定: `30`。0以下で無効化）。

### 4. 依存パッケージ（すでに導入済みなら不要）
```powershell
# bot用（Python 3.13の仮想環境）
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-bot.txt

# 文字起こし・要約用（Python 3.14）
py -3.14 -m pip install torch transformers google-generativeai python-dotenv

# （任意）話者分離を使う場合。Meet等、1本の音声に複数人の声が混ざっている場合に有効化する
py -3.14 -m pip install pyannote.audio

# ffmpeg（音声処理）
winget install Gyan.FFmpeg
```

---

## 使い方（Discord自動モード）

1. `議事録bot起動.bat` をダブルクリックして bot を起動（黒い画面が出たまま＝起動中）
2. Discord でボイスチャンネルに参加
3. テキストチャンネルで **`/record`** → 録音開始
4. 会議が終わったら **`/stop`** → 停止＆自動で議事録作成
5. しばらくすると、同じチャンネルに **議事録ファイル**が投稿されます

- 録音・議事録データは `recordings/<日時>/` に保存されます（Git管理外）。
- CPUで文字起こしするため、長い会議ほど時間がかかります。

---

## 使い方（手動モード）

Discord を使わず、手元の音声ファイルから議事録を作る場合：

1. `input/` フォルダに音声ファイル（mp3 / wav / m4a / mp4 / flac）を入れる
2. `run_pipeline.bat` を実行（`GEMINI_API_KEY` の設定が必要）
   - 文字起こしのみなら `文字起こし.bat`（音声をドラッグ＆ドロップでもOK）

Google Meet 等の商談・打ち合わせを録音した音声（自分と相手の声が1本にミックスされたファイル）にも使えます。
録音は OBS Studio 等でPCの「マイク＋スピーカー出力」をまとめて1ファイルに保存してください。
相手の発言（依頼されたタスク等）を録音・AI処理する旨は、事前に相手へ一言伝えておくことを推奨します。

### 話者分離（誰が話したか）を有効にする

デフォルトでは1本の音声から「誰が話したか」は区別できません。区別したい場合は以下を設定してください。

1. https://huggingface.co/pyannote/speaker-diarization-3.1 を開き、利用規約に同意（Hugging Face アカウントが必要）
2. https://huggingface.co/settings/tokens でアクセストークンを発行
3. `.env` の `HF_TOKEN` に貼り付け
4. `py -3.14 -m pip install pyannote.audio` を実行

設定済みの状態で `run_whisper.py`（＝`run_pipeline.bat` / `文字起こし.bat`）を実行すると、
書き起こしが `[時刻] 話者1: ...` のように発言者ラベル付きになり、`prompt.txt` の設定により
議事録内でも発言者ごとに整理されます（本人が名乗っていれば実名に、そうでなければ「話者1」等のまま）。
`HF_TOKEN` が未設定の場合は自動的にスキップされ、従来通りの動作になります。

---

## トラブルシューティング

- **bot がコマンドに反応しない** … 招待時に `applications.commands` スコープを付けたか確認。反映に少し時間がかかることがあります。
- **「ffmpegが見つかりません」** … `winget install Gyan.FFmpeg` 実行後、PCまたは端末を再起動。
- **議事録が作られず書き起こしだけできる** … `.env` の `GEMINI_API_KEY` が未設定です。
- **文字起こしが遅い** … GPUが無いためCPU処理になっています。短い会議で試すか、GPU環境の利用を検討してください。
- **話者分離で 401/403 エラーになる** … `pyannote/speaker-diarization-3.1` の利用規約に同意していないか、`HF_TOKEN` が間違っています。Hugging Face 上でモデルページを開き、同意した上でトークンを再発行してください。
- **話者分離が効いていない（発言者ラベルが付かない）** … `.env` の `HF_TOKEN` が空、または `pyannote.audio` が未インストールです。起動時ログに「話者分離は無効です」と出ていないか確認してください。
