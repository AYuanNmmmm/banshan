@echo off
chcp 65001 >nul
cd /d "%~dp0"

echo ============================================
echo   搬山 · 网盘迁移工具 —— 打包脚本
echo ============================================
echo.

rem ---- 找 Python ----
set "PY=%USERPROFILE%\.workbuddy\binaries\python\versions\3.13.12\python.exe"
if exist "%PY%" goto :found
set "PY=python"

:found
echo 使用 Python: %PY%
echo.

echo [1/3] 安装 PyInstaller ...
"%PY%" -m pip install --upgrade pyinstaller
if errorlevel 1 (
  echo [错误] PyInstaller 安装失败，请检查网络。
  pause
  exit /b 1
)
echo.

echo [2/3] 清理旧的打包产物 ...
if exist build rmdir /s /q build
if exist dist rmdir /s /q dist
echo.

echo [3/3] 开始打包（单文件 exe）...
"%PY%" -m PyInstaller panmigrate.spec
if errorlevel 1 (
  echo [错误] 打包失败。
  pause
  exit /b 1
)
echo.

echo ============================================
echo   打包完成！产物在 dist\ 目录：
echo     dist\搬山.exe
echo.
echo   双击 搬山.exe 即可启动，会自动打开浏览器。
echo ============================================
echo.
pause
