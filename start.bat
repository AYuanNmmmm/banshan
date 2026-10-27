@echo off
chcp 936 >nul
cd /d "%~dp0"
echo.
echo   网盘迁移工具启动中...
echo.

rem ---- 找 Python ----
set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if exist "%PY%" goto :found

set "PY=python"
python --version >nul 2>&1
if not errorlevel 1 goto :found

set "PY=py"
py --version >nul 2>&1
if not errorlevel 1 goto :found

echo [错误] 没找到 Python。请把 Python 加入 PATH，
echo   或把本文件里 PY 那一行改成你的 python.exe 完整路径。
echo.
pause
exit /b 1

:found
echo 使用: %PY%
echo 服务地址: http://127.0.0.1:8899
echo 停止服务请在本窗口按 Ctrl+C
echo.

"%PY%" -m panmigrate --port 8899
pause
