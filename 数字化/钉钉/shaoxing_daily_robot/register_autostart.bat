@echo off
setlocal
set "VBS=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup\shaoxing_listener.vbs"
set "PY=%USERPROFILE%\miniconda3\python.exe"
set "SCRIPT=%~dp0shaoxing_listener.py"
(
  echo Set WshShell = CreateObject("WScript.Shell"^)
  echo WshShell.Run """%PY%"" """%SCRIPT%""", 0, False
) > "%VBS%"
echo 已注册开机自启: %VBS%
start /b "" "%PY%" "%SCRIPT%"
echo 已后台启动 shaoxing_listener.py
pause
