@echo off
REM ── 自動建立 Windows 工作排程（每天 09:00 執行）────────────
REM 請以「系統管理員」身分執行此批次檔

set TASK_NAME=benz2288_price_tracker
set SCRIPT_DIR=%~dp0
set SCRIPT_PATH=%SCRIPT_DIR%run.bat

echo 建立工作排程：%TASK_NAME%
echo 執行時間：每天 09:00
echo 腳本路徑：%SCRIPT_PATH%
echo.

schtasks /create ^
  /tn "%TASK_NAME%" ^
  /tr "\"%SCRIPT_PATH%\"" ^
  /sc DAILY ^
  /st 09:00 ^
  /f ^
  /rl HIGHEST

if %errorlevel% == 0 (
    echo.
    echo ✅ 工作排程建立成功！
    echo    每天 09:00 會自動執行價格抓取
    echo.
    echo    查詢：schtasks /query /tn "%TASK_NAME%"
    echo    刪除：schtasks /delete /tn "%TASK_NAME%" /f
) else (
    echo.
    echo ❌ 建立失敗，請確認是否以「系統管理員」身分執行
)

pause
