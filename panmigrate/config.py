"""配置读写。凭据只落在本机 data/config.json，不上传任何地方。"""
from __future__ import annotations

import copy
import json
import os
import sys
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent


def _default_data_dir() -> Path:
    """数据目录：配置、任务库、登录缓存都放这里。

    优先级：
      1. 环境变量 PANMIGRATE_HOME（测试/多开用）
      2. **便携模式**：exe 旁边有 panmigrate-data 文件夹就用它
      3. 用户数据目录 %APPDATA%/panmigrate（打包后的默认）
      4. 项目内 data/（源码运行）

    第 2 条是为了「换电脑打开就能用」：把 搬山.exe 和 panmigrate-data 文件夹
    一起拷到新电脑（或 U 盘），账号登录态、任务进度、日志全都跟着走，
    不用重新扫码。什么都不放就还是走用户目录，升级程序也不会丢账号。
    """
    env = os.environ.get("PANMIGRATE_HOME")
    if env:
        return Path(env)
    # PyInstaller 打包后 sys.frozen 为 True，此时不往 exe 旁边写，改走用户目录
    if getattr(sys, "frozen", False):
        try:
            portable = Path(sys.executable).resolve().parent / "panmigrate-data"
        except OSError:
            portable = None
        if portable is not None and portable.is_dir():
            return portable
        if sys.platform == "win32":
            base = Path(os.environ.get("APPDATA", Path.home() / "AppData" / "Roaming"))
        elif sys.platform == "darwin":
            base = Path.home() / "Library" / "Application Support"
        else:
            base = Path(os.environ.get("XDG_DATA_HOME", Path.home() / ".local" / "share"))
        return base / "panmigrate"
    return BASE_DIR / "data"


DATA_DIR = _default_data_dir()
CONFIG_PATH = DATA_DIR / "config.json"

_lock = threading.Lock()

DEFAULTS: dict = {
    "source": {
        "provider": "baidu",
        "auth_mode": "qr",             # qr | cookie | oauth
        "client_id": "",
        "client_secret": "",
        "access_token": "",
        "refresh_token": "",
        "expires_at": 0,
        "cookie": "",
        "root_path": "/",
    },
    "target": {
        "provider": "aliyun",
        "auth_mode": "qr",             # qr | web_token | oauth
        "client_id": "",
        "client_secret": "",
        "access_token": "",
        "refresh_token": "",
        "expires_at": 0,
        "drive_id": "",
        "root_path": "/",
    },
    "transfer": {
        # 这三个默认值必须和前端「均衡」档一致（file 2 / part 2 / 10MB），
        # 否则用户一次都没改过，界面却显示「自定义」，会以为哪里坏了。
        "file_concurrency": 2,
        "part_concurrency": 2,
        "part_size_mb": 10,
        "bandwidth_kbps": 0,
        "retries": 3,
        "verify": True,
        # 目标端已有同名文件（内容不同）时：auto_rename 自动加序号 / skip 跳过 / refuse 报错
        "conflict": "auto_rename",
        # keep 保留源目录层级 / flat 全部平铺到目标目录
        "structure": "keep",
    },
    "schedule": {
        "enabled": False,
        "cron": "0 3 * * *",
    },
    # 网页登录（天翼一键登录 / 115·123 扫码）用哪个浏览器打开：
    #   auto     → 先用 Chrome/Edge；macOS 上没有它们才用 Safari（Windows 行为不变）
    #   chromium → 只用 Chrome/Edge
    #   safari   → 只用 Safari（仅 macOS 生效，其它平台自动当 auto 处理）
    "login": {
        "browser": "auto",
    },
    "last_paths": {
        "source": "/",
        "target": "/",
    },
    # 按网盘存凭据：切换 provider 时自动换用对应账号，不会把上一家的登录态冲掉
    "accounts": {},
}

# 需要在 accounts 里留存、且绝不能吐给前端的字段
CRED_KEYS = (
    "client_id",
    "client_secret",
    "access_token",
    "refresh_token",
    "expires_at",
    "cookie",
    "api_key",
    "drive_id",
)

SECRET_KEYS = ("client_secret", "access_token", "refresh_token", "cookie", "api_key")


def _merge(base: dict, override: dict) -> dict:
    out = copy.deepcopy(base)
    for key, value in (override or {}).items():
        if key in out and isinstance(out[key], dict) and isinstance(value, dict):
            out[key] = _merge(out[key], value)
        else:
            out[key] = value
    return out


def load() -> dict:
    with _lock:
        if not CONFIG_PATH.exists():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            CONFIG_PATH.write_text(
                json.dumps(DEFAULTS, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            return copy.deepcopy(DEFAULTS)
        try:
            raw = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            raw = {}
        return _merge(DEFAULTS, raw)


def save(cfg: dict) -> dict:
    with _lock:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        merged = _merge(DEFAULTS, cfg)
        CONFIG_PATH.write_text(
            json.dumps(merged, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return merged


def update(patch: dict) -> dict:
    """局部更新，只覆盖 patch 里出现的字段。"""
    current = load()
    return save(_merge(current, patch))


def masked(cfg: dict) -> dict:
    """给前端看的版本：密钥只留是否已填写的标记。"""
    out = copy.deepcopy(cfg)
    for side in ("source", "target"):
        block = out.get(side, {})
        for key in CRED_KEYS:
            if key not in SECRET_KEYS:
                continue
            raw = block.get(key) or ""
            block[key] = ""
            block[f"has_{key}"] = bool(raw)
        # 注意：必须用上面留下的 has_* 标记来判断。
        # 原来这里直接读 block["cookie"] / ["access_token"]，而它们刚被清空，
        # 结果 has_token 永远是 False —— 前端就永远显示「未授权」。
        block["has_token"] = bool(
            block["has_access_token"] or block["has_cookie"] or block["has_refresh_token"]
        )

    # accounts 里也是明文凭据，同样不能给前端
    accounts = out.get("accounts") or {}
    for prov, block in list(accounts.items()):
        if not isinstance(block, dict):
            continue
        accounts[prov] = {k: ("" if k in SECRET_KEYS else v) for k, v in block.items()}
    return out


def snapshot_side(cfg: dict, side: str) -> dict:
    """把某一端当前的凭据按 provider 归档，供以后切回来时自动恢复。"""
    block = cfg.get(side) or {}
    provider = (block.get("provider") or "").strip()
    if not provider:
        return cfg
    creds = {k: block[k] for k in CRED_KEYS if block.get(k) not in (None, "", 0)}
    if not creds:
        return cfg
    accounts = cfg.setdefault("accounts", {})
    accounts.setdefault(provider, {}).update(creds)
    return cfg


def restore_side(cfg: dict, side: str) -> dict:
    """切换 provider 后，把该网盘存过的凭据填回去；没存过则清空，避免串用别家的令牌。"""
    block = cfg.setdefault(side, {})
    provider = (block.get("provider") or "").strip()
    saved = ((cfg.get("accounts") or {}).get(provider) or {}) if provider else {}
    for key in CRED_KEYS:
        if key == "expires_at":
            block[key] = saved.get(key, 0)
        else:
            block[key] = saved.get(key, "")
    return cfg
