@echo off
cd /d "%~dp0"

REM ── 設定 Gmail App Password（只需設定一次）──────────────
REM 若已設定環境變數可刪除下面這行
REM set GMAIL_APP_PASSWORD=xxxx xxxx xxxx xxxx

echo [%date% %time%] 開始執行 benz2288 價格追蹤...
python tracker.py --push

echo.
echo 按任意鍵關閉...
pause > nul
