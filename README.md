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

---

## 構成

| ファイル | 役割 | 実行環境 |
|---|---|---|
| `discord_bot.py` | Discord録音bot本体（録音→保存→パイプライン起動） | `.venv`（Python 3.13） |
| `pipeline.py` | 文字起こし＋議事録作成（bot から呼ばれる） | Python 3.14（torch入り） |
| `run_whisper.py` | 手動：`input/` の音声を文字起こし | Python 3.14 |
| `run_gemini.py` | 手動：`output/` のテキストを要約 | Python 3.14 |
| `prompt.txt` | 議事録の書式・要約方針を指示するプロンプト | － |
| `議事録bot起動.bat` | bot をワンクリック起動 | － |

> 録音ライブラリ(Pycord)の都合で bot は **Python 3.13**、
> 文字起こし(torch)は既存の **Python 3.14** を使い、bot がサブプロセスで呼び分けます。

---

## セットアップ（初回のみ）

### 1. Discord bot を用意する
1. https://discord.com/developers/applications → **New Application**
2. 左メニュー **Bot** → **Reset Token** でトークンを取得（後で `.env` に貼る）
3. 同じ Bot 画面で **MESSAGE CONTENT INTENT** を ON
4. 左メニュー **OAuth2 → URL Generator**
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

### 4. 依存パッケージ（すでに導入済みなら不要）
```powershell
# bot用（Python 3.13の仮想環境）
py -3.13 -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements-bot.txt

# 文字起こし・要約用（Python 3.14）
py -3.14 -m pip install torch transformers google-generativeai python-dotenv

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

---

## トラブルシューティング

- **bot がコマンドに反応しない** … 招待時に `applications.commands` スコープを付けたか確認。反映に少し時間がかかることがあります。
- **「ffmpegが見つかりません」** … `winget install Gyan.FFmpeg` 実行後、PCまたは端末を再起動。
- **議事録が作られず書き起こしだけできる** … `.env` の `GEMINI_API_KEY` が未設定です。
- **文字起こしが遅い** … GPUが無いためCPU処理になっています。短い会議で試すか、GPU環境の利用を検討してください。
