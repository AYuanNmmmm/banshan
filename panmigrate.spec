# -*- mode: python ; coding: utf-8 -*-
"""搬山（网盘迁移工具）PyInstaller 打包配置。

只打包「代码 + 界面静态资源」，绝不打包任何数据：
  - 排除 panmigrate/data/**（天翼登录浏览器缓存）
  - 排除根目录 data/**（账号凭据 config.json、任务库 tasks.db）
  - 排除 tests/、vendor/、dom_dump.html

运行数据在运行时写到用户目录（见 config.py 的 DATA_DIR），
所以打出来的 exe/app 是「干净」的，不含任何账号信息。

打包命令：
  Windows:  pyinstaller panmigrate.spec
  macOS:    pyinstaller panmigrate.spec  （同一份 spec，跨平台通用）
"""
import os
import sys
from pathlib import Path

block_cipher = None

ROOT = Path(SPECPATH)  # spec 文件所在目录（项目根）
PKG = ROOT / "panmigrate"
VENDOR = ROOT / "vendor"

# 图标按平台挑：Windows 用 .ico，macOS 用 .icns。
# 注意 macOS 的 onefile 产物没有可挂图标的容器（图标只在 .app bundle 上生效），
# 这里仍然指对 .icns —— 以后要包 .app 时不用再改 spec。
ICON = ROOT / ("icon.icns" if sys.platform == "darwin" else "icon.ico")
if not ICON.is_file():
    ICON = ROOT / "icon.ico"

# 静态资源：界面 HTML/CSS/JS + 网盘 logo 图片，保持 web/ 目录结构
datas = [
    (str(PKG / "web" / "index.html"), "panmigrate/web"),
    (str(PKG / "web" / "style.css"), "panmigrate/web"),
    # 两套主题皮肤是独立文件，必须一起打进去，否则打包后主题切换会 404
    (str(PKG / "web" / "theme-light.css"), "panmigrate/web"),
    (str(PKG / "web" / "theme-dark.css"), "panmigrate/web"),
    (str(PKG / "web" / "app.js"), "panmigrate/web"),
    (str(PKG / "web" / "tylogin.js"), "panmigrate/web"),
    (str(PKG / "web" / "logo"), "panmigrate/web/logo"),
]

# vendor/qrcode 是纯 Python 二维码库，扫码登录（阿里/夸克）要用。
# 通过 pathex 让 PyInstaller 把它当普通模块打包进去，扫码才有二维码。
hiddenimports = [
    "qrcode",
    "qrcode.image.svg",
    "qrcode.image.base",
    "qrcode.image.pure",
    # adapters/__init__.py 里是「用到才 import」的延迟导入，虽然 PyInstaller
    # 通常也能扫到函数体内的 import，但漏了就是运行时才炸「模块不存在」，
    # 这种问题在打包产物里极难排查，宁可显式列出来。
    "panmigrate.adapters.pan123",
    "panmigrate.adapters.pan115",
]

a = Analysis(
    [str(ROOT / "run.py")],
    pathex=[str(ROOT), str(VENDOR)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        # 明确排除可能被自动探测进来的测试/无关模块
        "pytest",
        "setuptools",
        "pip",
        "unittest",
    ],
    win_no_prefer_redirects=False,
    win_private_assemblies=False,
    cipher=block_cipher,
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.zipfiles,
    a.datas,
    [],
    name="搬山",           # 产出的可执行文件名
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,           # 不压缩，避免误报病毒
    upx_exclude=[],
    runtime_tmpdir=None,
    console=True,        # 保留控制台窗口（能看日志、Ctrl+C 停止）；False 则纯后台无窗口
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # 图标：应用图标（网页 logo 的蓝紫渐变方块 + 旭日 + 双层山体）
    icon=str(ICON),
)
