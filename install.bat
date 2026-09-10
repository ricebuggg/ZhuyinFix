@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo === ZhuyinFix 安裝 ===

where uv >nul 2>nul
if %errorlevel%==0 goto have_uv
if exist "%USERPROFILE%\.local\bin\uv.exe" goto have_uv
if exist "%USERPROFILE%\.aki\bin\uv.exe" goto have_uv
echo [1/3] 安裝 uv（Python 執行環境管理工具）...
powershell -NoProfile -ExecutionPolicy Bypass -Command "irm https://astral.sh/uv/install.ps1 | iex"
if not exist "%USERPROFILE%\.local\bin\uv.exe" (
  echo uv 安裝失敗，請手動安裝：https://docs.astral.sh/uv/
  pause & exit /b 1
)
:have_uv
echo [1/3] uv OK

echo [2/3] 建立開機自動啟動捷徑...
set "LNK=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ZhuyinFix.lnk"
powershell -NoProfile -Command "$q=[char]34; $s=(New-Object -ComObject WScript.Shell).CreateShortcut('%LNK%'); $s.TargetPath='wscript.exe'; $s.Arguments=$q+'%~dp0ZhuyinFix.vbs'+$q; $s.WorkingDirectory='%~dp0'; $s.WindowStyle=7; $s.Description='ZhuyinFix'; $s.Save()"
if exist "%LNK%" (echo       已加入啟動資料夾) else (echo       捷徑建立失敗，請手動把 ZhuyinFix.bat 捷徑放到 shell:startup)

echo [3/3] 立即啟動...
call ZhuyinFix.bat
echo.
echo 完成。在任何視窗打亂碼後按 Ctrl+Shift+Z 試試（第一次啟動要幾秒下載套件）。
pause
