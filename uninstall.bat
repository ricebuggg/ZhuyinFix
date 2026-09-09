@echo off
chcp 65001 >nul
del "%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\ZhuyinFix.lnk" 2>nul
taskkill /f /fi "WINDOWTITLE eq ZhuyinFix*" >nul 2>nul
powershell -NoProfile -Command "Get-CimInstance Win32_Process | ? { $_.CommandLine -like '*zhuyinfix.py*' } | % { Stop-Process -Id $_.ProcessId -Force }"
echo 已移除開機啟動並停止程式。
pause
