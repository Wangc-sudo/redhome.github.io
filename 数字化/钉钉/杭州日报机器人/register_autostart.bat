@echo off
rem 注册杭州日报监听机器人到当前用户启动文件夹（静默常驻）
set "STARTUP=%APPDATA%\Microsoft\Windows\Start Menu\Programs\Startup"
set "PY=%USERPROFILE%\miniconda3\pythonw.exe"
if not exist "%PY%" set "PY=%USERPROFILE%\miniconda3\python.exe"

> "%STARTUP%\hangzhou_listener_start.vbs" (
    echo Set WshShell = CreateObject^("WScript.Shell"^)
    echo WshShell.Run """%PY%"" ""%~dp0hangzhou_listener.py""", 0, False
)
echo 已注册开机自启: %STARTUP%\hangzhou_listener_start.vbs
rem 立即启动一次
start "" "%STARTUP%\hangzhou_listener_start.vbs"
echo 监听进程已启动（后台静默）
pause
