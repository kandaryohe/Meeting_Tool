@echo off
chcp 65001 > nul
pushd "%~dp0"

echo ========================================
echo   Discord 議事録bot を起動します
echo ========================================
echo.

if not exist ".venv\Scripts\python.exe" (
    echo [エラー] 仮想環境 .venv が見つかりません。
    echo 先にセットアップを行ってください（py -3.13 -m venv .venv など）。
    pause
    popd
    exit /b 1
)

if not exist ".env" (
    echo [エラ] .env が見つかりません。
    echo .env.example を .env にコピーし、DISCORD_TOKEN と GEMINI_API_KEY を設定してください。
    pause
    popd
    exit /b 1
)

echo botを起動します。停止するには、このウィンドウで Ctrl + C を押してください。
echo.
.venv\Scripts\python.exe discord_bot.py

echo.
echo botが終了しました。
popd
pause
