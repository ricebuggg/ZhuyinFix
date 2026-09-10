@echo off
cd /d "%~dp0"
where uv >nul 2>nul && (set "UV=uv") || if exist "%USERPROFILE%\.local\bin\uv.exe" (set "UV=%USERPROFILE%\.local\bin\uv.exe") else (set "UV=%USERPROFILE%\.aki\bin\uv.exe")
"%UV%" run --with pynput --with pystray --with pillow python zhuyinfix.py
pause
