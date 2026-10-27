@echo off
chcp 936 >nul
cd /d "%~dp0"
echo 运行 panmigrate 测试（纯标准库，无需安装依赖）
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
echo.
"%PY%" -W ignore::ResourceWarning -m unittest discover -s tests -t . -v %*
set RC=%errorlevel%

echo.
echo ----------------------------------------
if "%RC%"=="0" (echo 全部通过) else (echo 测试失败，请看上面 FAIL / ERROR 的行)
echo.
pause
