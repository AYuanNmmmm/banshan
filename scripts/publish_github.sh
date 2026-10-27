#!/bin/bash
# 一条命令把仓库推到 GitHub 并发布 Release（在**你自己的终端**里跑，不要在本工具的沙箱里跑）。
#
#   bash scripts/publish_github.sh                 # 仓库名用默认 banshan
#   bash scripts/publish_github.sh my-repo-name    # 自定义仓库名
#
# 它会依次做：
#   1. 找 gh；没有就自动下载 macOS arm64 版解压到 ~/.local/opt/gh（不用密码、不动系统目录）
#   2. 没登录就调起 gh auth login（浏览器里点一下 + 按提示输入一次性代码）
#   3. 建公开仓库（已存在则跳过），并把 origin 指过去
#   4. 用你 GitHub 账号的 noreply 邮箱重写本次提交署名（免得显示成「banshan」这个占位名）
#   5. push main
#   6. 发 Release v1.26.0，附上 macOS 包 dist/搬山-macOS-arm64-v1.26.0.zip
#
# 任何一步失败都会停下并说明原因。
set -euo pipefail
cd "$(dirname "$0")/.."

REPO="${1:-banshan}"
DESC="搬山 · 网盘迁移工具 —— 六家网盘互传，本机中转、分片直传、断点续传"
TAG="v1.26.0"
ASSET="dist/搬山-macOS-arm64-v1.26.0.zip"
GH="$HOME/.local/opt/gh/bin/gh"
GH_VERSION="2.103.3"

say() { printf "\n\033[1m== %s ==\033[0m\n" "$1"; }
die() { printf "\n\033[31m✗ %s\033[0m\n" "$1" >&2; exit 1; }

# ---------- 1. gh ----------
say "1/6 检查 gh"
if command -v gh >/dev/null 2>&1; then
  GH="$(command -v gh)"
elif [ -x "$GH" ]; then
  :
else
  echo "没装 gh，正在下载（约 13MB，解压到 ~/.local/opt/gh）…"
  mkdir -p "$HOME/.local/opt"
  TMP="$(mktemp -d)"
  ARCH="$(uname -m)"; [ "$ARCH" = "arm64" ] || ARCH="amd64"
  URL="https://github.com/cli/cli/releases/download/v${GH_VERSION}/gh_${GH_VERSION}_macOS_${ARCH}.zip"
  curl -fL --retry 3 --progress-bar -o "$TMP/gh.zip" "$URL" || die "下载失败，检查网络后重试"
  unzip -q "$TMP/gh.zip" -d "$TMP"
  rm -rf "$HOME/.local/opt/gh"
  mv "$TMP/gh_${GH_VERSION}_macOS_${ARCH}" "$HOME/.local/opt/gh"
  rm -rf "$TMP"
  [ -x "$GH" ] || die "解压后没找到 $GH"
fi
echo "gh $("$GH" --version | head -1)"

# ---------- 2. 登录 ----------
say "2/6 检查 GitHub 登录状态"
if ! "$GH" auth status >/dev/null 2>&1; then
  echo "需要登录一次：按提示选 GitHub.com → HTTPS → Login with a web browser"
  "$GH" auth login -h github.com -p https -w || die "登录没完成"
fi
"$GH" auth status 2>&1 | head -3

# ---------- 3. 仓库 ----------
say "3/6 准备仓库 $REPO"
OWNER="$("$GH" api user --jq .login)"
if "$GH" repo view "$OWNER/$REPO" >/dev/null 2>&1; then
  echo "已存在 $OWNER/$REPO，直接推送"
  git remote get-url origin >/dev/null 2>&1 \
    && git remote set-url origin "https://github.com/$OWNER/$REPO.git" \
    || git remote add origin "https://github.com/$OWNER/$REPO.git"
else
  "$GH" repo create "$REPO" --public --description "$DESC" \
      --source=. --remote=origin || die "建仓库失败"
fi

# ---------- 4. 提交署名 ----------
say "4/6 把提交署名改成你的 GitHub 账号"
GH_ID="$("$GH" api user --jq .id)"
git config user.name "$OWNER"
git config user.email "${GH_ID}+${OWNER}@users.noreply.github.com"
git commit --amend --reset-author --no-edit
echo "署名：$OWNER <${GH_ID}+${OWNER}@users.noreply.github.com>"

# ---------- 5. push ----------
say "5/6 推送到 GitHub"
git push -u origin main

# ---------- 6. Release ----------
say "6/6 发布 $TAG"
if [ -f "$ASSET" ]; then
  if "$GH" release view "$TAG" >/dev/null 2>&1; then
    "$GH" release upload "$TAG" "$ASSET" --clobber
  else
    "$GH" release create "$TAG" "$ASSET" \
        --title "搬山 ${TAG} · 首次开源" \
        --notes-file "docs/release-notes/${TAG}.md"
  fi
  echo "附件：$ASSET"
else
  echo "没找到 $ASSET，先跑 bash build_mac.sh 再发 Release"
fi

printf "\n\033[32m完成：https://github.com/%s/%s\033[0m\n" "$OWNER" "$REPO"
