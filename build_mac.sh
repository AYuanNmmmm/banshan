#!/bin/bash
# 搬山 · macOS 打包脚本（只在 macOS 上跑，PyInstaller 不支持交叉打包）
#
#   bash build_mac.sh            # 出 dist/搬山 + dist/搬山.app
#   bash build_mac.sh --skip-test # 跳过测试（不推荐）
#
# 产物：
#   dist/搬山       单文件可执行（arm64），终端里 ./搬山 启动
#   dist/搬山.app   应用包，双击启动、Dock 有图标；界面右上角「退出」可正常结束进程
#
# 注意：图标（icon.icns）只用一次，之后改图标要重新生成 iconset 再 iconutil。
set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="搬山"
VERSION="$(python3 -c 'import panmigrate; print(panmigrate.__version__)')"
echo "== 版本 $VERSION =="

if [ "${1:-}" != "--skip-test" ]; then
  echo "== 1/4 跑测试 =="
  python3 -W ignore::ResourceWarning -m unittest discover -s tests -t . 2>&1 | tail -3
fi

echo "== 2/4 PyInstaller 打包（先清 build/dist，避免复用缓存导致图标/资源不更新）=="
rm -rf build dist
python3 -m PyInstaller panmigrate.spec --noconfirm --clean 2>&1 | tail -3

echo "== 3/4 组装 $APP_NAME.app =="
APP="dist/$APP_NAME.app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "dist/$APP_NAME" "$APP/Contents/MacOS/$APP_NAME"
cp icon.icns "$APP/Contents/Resources/$APP_NAME.icns"
printf 'APPL????' > "$APP/Contents/PkgInfo"
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>$APP_NAME</string>
  <key>CFBundleDisplayName</key><string>$APP_NAME</string>
  <key>CFBundleIdentifier</key><string>com.yuan.banshan</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>$APP_NAME</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleSignature</key><string>????</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleInfoDictionaryVersion</key><string>6.0</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSApplicationCategoryType</key><string>public.app-category.utilities</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSHumanReadableCopyright</key><string>搬山 · 网盘迁移工具</string>
</dict>
</plist>
PLIST
chmod +x "$APP/Contents/MacOS/$APP_NAME"

echo "== 4/4 本地签名（ad-hoc，够本机双击运行；换机器要重新签名或右键打开）=="
codesign --force --deep --sign - "$APP" 2>&1 | tail -2

# Release 附件名必须是 ASCII：GitHub 会把中文从附件名里剔除，变成「-macOS-arm64-….zip」
ZIP="dist/banshan-macOS-arm64-v${VERSION}.zip"
rm -f "$ZIP"
( cd dist && zip -qry "$(basename "$ZIP")" "$APP_NAME.app" )

echo
echo "完成："
echo "  dist/$APP_NAME        $(du -h "dist/$APP_NAME" | cut -f1)"
echo "  dist/$APP_NAME.app    $(du -sh "$APP" | cut -f1)"
echo "  $ZIP    $(du -h "$ZIP" | cut -f1)"
