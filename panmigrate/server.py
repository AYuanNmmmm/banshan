"""本地网页服务：把引擎包一层 HTTP，浏览器里就能操作。"""
from __future__ import annotations

import json
import mimetypes
import os
import shutil
import subprocess
import sys
import threading
import time
import traceback
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from . import __version__, adapters, config, qrlogin, safari, tylogin, web_login
from .adapters import registry
from .adapters.mock import seed_demo_files
from .db import TaskDB
from .engine import TransferEngine
from .scheduler import CronScheduler

WEB_DIR = Path(__file__).resolve().parent / "web"
MAX_BODY = 1 << 20

# 网页登录（天翼一键登录 / 115·123 扫码）用的受控浏览器：优先 Edge，再 Chrome。
# **顺序和内容都要保持向后兼容**：Windows 上原来能用的路径必须原样命中。
_EDGE_CANDIDATES = [
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
]

# macOS 上 Chrome / Edge 的位置。这里只追加，不影响 Windows 的查找顺序。
_MAC_EDGE_CANDIDATES = [
    "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
    "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
    str(Path.home() / "Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge"),
    str(Path.home() / "Applications/Google Chrome.app/Contents/MacOS/Google Chrome"),
]


def _chromium_candidates() -> list[str]:
    """当前平台可用的 Chromium 系浏览器路径（Windows 保持原清单）。"""
    if sys.platform == "darwin":
        return list(_MAC_EDGE_CANDIDATES)
    if sys.platform.startswith("win"):
        return list(_EDGE_CANDIDATES)
    # Linux 等其它平台：只认 PATH 里的常见命令名，找不到就用不了 CDP 通道
    return [p for p in (shutil.which("google-chrome"), shutil.which("chromium"),
                        shutil.which("microsoft-edge")) if p]


class App:
    def __init__(self, db_path: Path | None = None):
        self.cfg = config.load()
        # db_path 只是给测试用的注入口，正常运行走默认 data/tasks.db
        self.db = TaskDB(db_path)
        self.engine = TransferEngine(self.db, self.cfg.get("transfer") or {})
        self.source = None
        self.sink = None
        self._stamp = ""
        # 进程启动时间：界面上显示出来，就能分辨看到的是新开的还是昨天那个旧实例
        self.started_at = time.strftime("%Y-%m-%d %H:%M:%S")
        self.pid = os.getpid()
        self.errors = {"source": "", "target": ""}
        self._lock = threading.RLock()

        sched = self.cfg.get("schedule") or {}
        self.scheduler = CronScheduler(self._scheduled_job)
        self.scheduler.configure(sched.get("enabled"), sched.get("cron"))
        self.scheduler.start()

        self._scan_thread = None
        self._scan_error = ""
        self._scan_result = None
        self._quota_cache = {"at": 0.0, "data": None}
        self.qr_sessions: dict[str, object] = {}
        # 天翼「一键登录」状态：None 表示没有进行中的会话
        self._tylogin: dict | None = None
        # 扫码登录（123 / 115）的会话状态，和天翼分开存，互不干扰
        self._weblogin: dict | None = None
        self.ensure_adapters()

    # ---------------- 适配器 ----------------
    def ensure_adapters(self) -> None:
        self.engine.settings = dict(self.cfg.get("transfer") or {})
        self._normalize_modes()
        stamp = json.dumps(
            {"s": self.cfg.get("source"), "t": self.cfg.get("target")},
            sort_keys=True,
            default=str,
        )
        if stamp == self._stamp:
            return
        if self.engine.running and self.source is not None:
            # 迁移进行中不换适配器，等这一轮结束再重建，避免把引擎手里的对象换掉
            return
        self._stamp = stamp
        self.errors = {"source": "", "target": ""}
        self._quota_cache = {"at": 0.0, "data": None}
        try:
            self.source = adapters.build_source(self.cfg["source"])
        except Exception as exc:
            self.source = None
            self.errors["source"] = str(exc)
        try:
            self.sink = adapters.build_sink(self.cfg["target"])
        except Exception as exc:
            self.sink = None
            self.errors["target"] = str(exc)
        for side, adapter in (("source", self.source), ("target", self.sink)):
            if adapter is not None:
                adapter.on_token = lambda tokens, _s=side: self._persist_tokens(_s, tokens)
        self.engine.attach(
            self.source,
            self.sink,
            self.cfg["source"].get("root_path") or "/",
            self.cfg["target"].get("root_path") or "/",
        )

    def _normalize_modes(self) -> None:
        """旧配置文件里的认证方式可能已不适用于当前网盘，纠正成它支持的第一个。"""
        changed = False
        for side in ("source", "target"):
            block = self.cfg.get(side) or {}
            prov = registry.get(block.get("provider") or "mock")
            modes = prov.get("auth_modes") or ["none"]
            if (block.get("auth_mode") or "") not in modes:
                block["auth_mode"] = modes[0]
                changed = True
        if changed:
            self.cfg = config.update(
                {"source": self.cfg.get("source") or {}, "target": self.cfg.get("target") or {}}
            )

    def _persist_tokens(self, side: str, tokens: dict) -> None:
        """把续期后的令牌落盘 —— 阿里云的 refresh_token 会轮换，
        不写回去下次启动就用旧的，会失效。"""
        patch = {
            key: tokens[key]
            for key in ("access_token", "refresh_token", "expires_at")
            if key in tokens
        }
        if not patch:
            return
        self.cfg = config.update({side: patch})
        config.snapshot_side(self.cfg, side)   # 归档到该网盘的账号里，切走再切回来还能用
        self.cfg = config.save(self.cfg)
        self._stamp = ""

    # ---------------- 扫码登录 ----------------
    def qr_start(self, role: str) -> dict:
        cfg = self.cfg.get(role) or {}
        provider = cfg.get("provider") or ""
        session = qrlogin.create(provider, role)
        now = time.time()
        for sid, old in list(self.qr_sessions.items()):
            if now - getattr(old, "created_at", now) > 900:
                self.qr_sessions.pop(sid, None)
        self.qr_sessions[session.sid] = session
        return session.start()

    def qr_poll(self, sid: str) -> dict:
        session = self.qr_sessions.get(sid)
        if session is None:
            raise RuntimeError("二维码已失效，请重新获取")
        result = session.poll()
        session.polls = getattr(session, "polls", 0) + 1
        result["raw"] = getattr(session, "last_raw", {})
        result["polls"] = session.polls
        status = result.get("status") or ""
        if status != getattr(session, "last_status", ""):
            session.last_status = status
            self.engine.log(f"[扫码] {session.label} -> {status} {result['raw']}", "info")
        if result.get("done") and getattr(session, "credentials", None):
            self._apply_qr(session.role, session.credentials)
            self.qr_sessions.pop(sid, None)
        return result

    def qr_debug(self) -> dict:
        now = time.time()
        return {
            "sessions": [
                {
                    "sid": s.sid,
                    "provider": getattr(s, "provider", ""),
                    "role": s.role,
                    "age": int(now - s.created_at),
                    "polls": getattr(s, "polls", 0),
                    "last_status": getattr(s, "last_status", ""),
                    "raw": getattr(s, "last_raw", {}),
                    "bizext": getattr(s, "bizext_raw", ""),
                }
                for s in self.qr_sessions.values()
            ]
        }

    def _apply_qr(self, role: str, creds: dict) -> None:
        patch = {"auth_mode": "qr"}
        if creds.get("cookie"):
            patch["cookie"] = creds["cookie"]
        if creds.get("refresh_token"):
            patch["refresh_token"] = creds["refresh_token"]
            patch["access_token"] = ""
            patch["expires_at"] = 0
        self.cfg = config.update({role: patch})
        config.snapshot_side(self.cfg, role)
        self.cfg = config.save(self.cfg)
        self._stamp = ""
        if not self.engine.running:
            self.ensure_adapters()
        self.engine.log(f"{'源' if role == 'source' else '目标'}账号已通过扫码登录")

    # ---------------- 天翼「一键登录」（内嵌浏览器自动抓 cookie） ----------------
    def _find_edge(self) -> str:
        """老接口，保持原样：只找 Chromium 系浏览器（Windows 行为不变）。"""
        for path in _chromium_candidates():
            if os.path.isfile(path):
                return path
        return ""

    def _login_browser(self) -> tuple[str, str, str]:
        """选登录用的浏览器，返回 (backend, 可执行路径, 显示名)。

        选择顺序（**Windows 与改动前完全一致**）：
          1. 配置 login.browser = "chromium" → 只用 Edge/Chrome；
          2. 配置 login.browser = "safari" → 仅 macOS 且 safaridriver 存在时用 Safari，
             其它平台忽略这个值（按 auto 处理），不会因为同步过来的配置把 Windows 弄坏；
          3. auto：先 Edge/Chrome（Windows 上就是你原来那两条路），
             **只有 macOS** 在没有 Chromium 浏览器时才退到 Safari。
        """
        pref = str(((self.cfg.get("login") or {}).get("browser") or "auto")).strip().lower()
        mac_safari_ok = sys.platform == "darwin" and bool(safari.driver_path())

        if pref == "safari" and mac_safari_ok:
            return "safari", safari.driver_path(), "Safari"

        for path in _chromium_candidates():
            if os.path.isfile(path):
                label = "Edge" if "Edge" in path or "edge" in path else "Chrome"
                return "cdp", path, label

        # 只剩 Safari 这条路（且必须是 mac）
        if mac_safari_ok and pref != "chromium":
            return "safari", safari.driver_path(), "Safari"
        return "", "", ""

    def login_capability(self) -> dict:
        """给界面看的登录浏览器能力表：哪些能选、当前会选谁。"""
        backend, _path, label = self._login_browser()
        chromium = ""
        for path in _chromium_candidates():
            if os.path.isfile(path):
                chromium = os.path.basename(path)
                break
        return {
            "platform": sys.platform,
            "current": backend or "",
            "current_label": label,
            "safari_supported": sys.platform == "darwin" and bool(safari.driver_path()),
            "chromium_found": bool(chromium),
            "chromium_label": chromium,
            "safari_hint": safari.ENABLE_HINT if sys.platform == "darwin" else "",
        }

    def _find_node(self) -> str:
        # node 用于跑 CDP 客户端脚本（node v22 原生带 WebSocket）
        # 先查 PATH，再查常见安装位置（含 WorkBuddy 自带的私有 node）
        for cmd in ("node", "node.exe"):
            p = shutil.which(cmd)
            if p:
                return p
        candidates = [
            # WorkBuddy 私有 node（按版本目录下所有 node.exe）
            Path.home() / ".workbuddy" / "binaries" / "node",
            # 常见全局安装位置
            Path("C:/Program Files/nodejs/node.exe"),
            Path("C:/Program Files (x86)/nodejs/node.exe"),
        ]
        for base in candidates[:1]:
            try:
                if base.is_dir():
                    for p in sorted(base.rglob("node.exe"), reverse=True):
                        return str(p)
            except OSError:
                continue
        for p in candidates[1:]:
            if p.is_file():
                return str(p)
        return ""

    def tylogin_start(self, role: str) -> dict:
        """天翼一键登录：拉起受控浏览器 → 用户登录 → 经 CDP 读出登录态。

        走纯 Python 的 CDP 客户端（tylogin.py + cdp.py），**不依赖 node** ——
        这样 exe 拷到任何一台 Windows 上都能直接用。
        """
        if self._tylogin and self._tylogin.get("active"):
            return {"ok": False, "error": "已有一个登录窗口在进行中，请先完成或取消"}
        backend, browser, label = self._login_browser()
        if not backend:
            return {"ok": False, "error": "没找到 Edge/Chrome 浏览器，无法启动自动登录"}
        edge = browser

        # 浏览器 profile 写到数据目录（打包后是用户目录，可写；不进 exe 内部）
        profile = (config.DATA_DIR / ".tylogin-profile").resolve()
        # 用一个空闲端口，避免和别的进程冲突
        import socket as _socket
        with _socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        runner = tylogin.TyLogin(edge, profile, port, backend=backend)
        state = {
            "runner": runner,
            "phase": "starting",
            "role": role,
            "error": "",
            "cookie": "",
            "active": True,
            "profile": str(profile),
            "port": port,
            "browser": label,
        }
        self._tylogin = state

        def _on_phase(name: str) -> None:
            if self._tylogin is state:
                state["phase"] = name

        def _run():
            try:
                cookie = runner.run(on_phase=_on_phase)
                state["cookie"] = cookie
                state["account"] = getattr(runner, "account", "")
                state["phase"] = "done"
                self._apply_tylogin(role, cookie)
            except Exception as exc:  # noqa: BLE001
                state["error"] = str(exc) or repr(exc) or type(exc).__name__
                state["phase"] = "error"
            finally:
                state["active"] = False

        threading.Thread(target=_run, name="tylogin", daemon=True).start()
        hint = f"已用 {label} 打开登录窗口，请在窗口里登录天翼云盘"
        if backend == "safari":
            hint += "（Safari 需要在「开发」菜单里允许远程自动化）"
        return {"ok": True, "phase": "starting", "hint": hint, "browser": label}

    def _apply_tylogin(self, role: str, cookie: str) -> None:
        patch = {"auth_mode": "cookie", "cookie": cookie}
        self.cfg = config.update({role: patch})
        config.snapshot_side(self.cfg, role)
        self.cfg = config.save(self.cfg)
        self._stamp = ""
        if not self.engine.running:
            self.ensure_adapters()
        self.engine.log(f"{'源' if role == 'source' else '目标'}账号已通过一键登录（自动抓取 cookie）")

    def tylogin_cancel(self) -> dict:
        """取消天翼一键登录：让轮询退出并关掉那个受控浏览器窗口。

        没有这个口子的话，用户中途想放弃只能等 5 分钟超时，
        期间再点登录会被「已有一个登录窗口在进行中」挡住。
        """
        st = self._tylogin
        if not st:
            return {"ok": True}
        runner = st.get("runner")
        if runner is not None:
            try:
                runner.stop()
            except Exception:
                pass
        st["active"] = False
        st["phase"] = "cancelled"
        return {"ok": True, "msg": "已取消"}

    def tylogin_status(self) -> dict:
        if not self._tylogin:
            return {"active": False}
        st = self._tylogin
        out = {
            "active": bool(st.get("active")),
            "phase": st.get("phase", ""),
            "error": st.get("error", ""),
            # 登录成功后带上账号（脱敏手机号/邮箱），界面能显示「已登录 xxx」
            "account": st.get("account", ""),
        }
        if st.get("phase") == "done":
            out["done"] = True
            # 完成后清空状态，允许再次发起
            self._tylogin = None
        return out

    # ---------------- 扫码登录（123 云盘 / 115 网盘） ----------------
    def weblogin_start(self, role: str, site: str = "") -> dict:
        """拉起受控浏览器让用户扫码，登录态取回后自动写进配置。

        和天翼的区别只在「登录态存在哪」：115 在 cookie，123 在 localStorage 的
        authorToken。取法不同，流程完全一致。
        """
        if self._weblogin and self._weblogin.get("active"):
            return {"ok": False, "error": "已有一个扫码窗口在进行中，请先完成或取消"}

        if not site:
            site = ((self.cfg.get(role) or {}).get("provider") or "").strip()
        if site not in web_login.SITES:
            return {"ok": False, "error": f"{site or '当前网盘'} 不支持扫码登录"}

        backend, browser, label = self._login_browser()
        if not backend:
            return {"ok": False, "error": "没找到 Edge/Chrome 浏览器，无法启动扫码登录"}
        edge = browser

        # profile 按站点分开，互不串号；复用同一个目录，第二次登录几乎瞬间完成
        profile = (config.DATA_DIR / f".weblogin-profile-{site}").resolve()
        import socket as _socket

        with _socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            port = sock.getsockname()[1]

        runner = web_login.WebLogin(site, edge, profile, port, backend=backend)
        state = {
            "runner": runner,
            "phase": "starting",
            "role": role,
            "site": site,
            "error": "",
            "account": "",
            "active": True,
            "browser": label,
        }
        self._weblogin = state

        def _on_phase(name: str) -> None:
            if self._weblogin is state:
                state["phase"] = name

        def _run():
            try:
                got = runner.run(on_phase=_on_phase)
                state["account"] = got.get("account") or ""
                state["phase"] = "done"
                self._apply_weblogin(role, site, got["mode"], got["value"], got.get("account") or "")
            except Exception as exc:  # noqa: BLE001
                state["error"] = str(exc) or repr(exc) or type(exc).__name__
                state["phase"] = "error"
            finally:
                state["active"] = False

        threading.Thread(target=_run, name="weblogin", daemon=True).start()
        site_label = web_login.SITES[site]["label"]
        hint = f"已用 {label} 打开登录窗口，请用手机扫码登录{site_label}"
        if backend == "safari":
            hint += "（Safari 需要在「开发」菜单里允许远程自动化）"
        return {"ok": True, "phase": "starting", "hint": hint, "browser": label}

    def _apply_weblogin(
        self, role: str, site: str, mode: str, value: str, account: str = ""
    ) -> None:
        """登录态落库：123 存 access_token，115 存 cookie。"""
        if mode == "token":
            patch = {"auth_mode": "web_qr", "access_token": value}
        else:
            patch = {"auth_mode": "cookie", "cookie": value}
        # 账号名不敏感，明文存下来，界面能显示「已登录 138****0000」
        if account:
            patch["account_name"] = account
        self.cfg = config.update({role: patch})
        config.snapshot_side(self.cfg, role)
        self.cfg = config.save(self.cfg)
        self._stamp = ""
        if not self.engine.running:
            self.ensure_adapters()
        label = web_login.SITES[site]["label"]
        self.engine.log(
            f"{'源' if role == 'source' else '目标'}账号已通过扫码登录（{label}，自动抓取登录态）"
        )

    def weblogin_status(self) -> dict:
        if not self._weblogin:
            return {"active": False}
        st = self._weblogin
        out = {
            "active": bool(st.get("active")),
            "phase": st.get("phase", ""),
            "error": st.get("error", ""),
            "account": st.get("account", ""),
            "site": st.get("site", ""),
        }
        if st.get("phase") in ("done", "error"):
            out["done"] = st.get("phase") == "done"
            # 收尾后清空，允许再次发起
            self._weblogin = None
        return out

    def weblogin_cancel(self) -> dict:
        if self._weblogin and self._weblogin.get("runner"):
            try:
                self._weblogin["runner"].stop()
            except Exception:
                pass
            return {"ok": True}
        return {"ok": False, "error": "没有进行中的扫码登录"}

    def run_demo(self) -> dict:
        """本地演示：只在「还没配过凭据」的那一端切到演示盘。

        之前这里无条件把两端都改成 mock，会直接把用户已经登录好的网盘覆盖掉，
        属于破坏性操作，必须收窄。
        """
        created = seed_demo_files()
        patch: dict = {}
        switched: list[str] = []
        for side in ("source", "target"):
            block = self.cfg.get(side) or {}
            authorized = bool(
                (block.get("cookie") or "").strip()
                or (block.get("access_token") or "").strip()
                or (block.get("refresh_token") or "").strip()
            )
            if not authorized:
                patch[side] = {"provider": "mock", "auth_mode": "none", "root_path": "/"}
                switched.append(side)
        if patch:
            self.cfg = config.update(patch)
            self._stamp = ""
            self.ensure_adapters()
        message = (
            "已生成演示文件"
            if switched
            else "两端都已登录真实网盘，演示模式已跳过（不会改动你的配置）"
        )
        return {"ok": True, "files": created, "switched": switched, "message": message}

    MAX_PICK_FILES = 5000

    def _part_mb(self) -> int:
        return int((self.cfg.get("transfer") or {}).get("part_size_mb") or 8)

    def _rel_of(self, path: str) -> str:
        """把源端的绝对路径换算成「相对迁移源目录」的路径，目标端就按这个还原结构。"""
        root = (self.cfg["source"].get("root_path") or "/").rstrip("/")
        if root and path.startswith(root + "/"):
            return path[len(root):].lstrip("/")
        return path.lstrip("/")

    def _entry(self, rel: str, name: str, src_id: str, size: int, part_mb: int) -> dict:
        from .engine import calc_parts

        part_size, total = calc_parts(size, part_mb)
        return {
            "rel_path": rel,
            "name": name,
            "src_id": str(src_id or ""),
            "size": size,
            "part_size": part_size,
            "total_parts": total,
            "skip": False,
        }

    def _expand_dir(self, path: str, part_mb: int, out: list) -> bool:
        """递归展开一个文件夹，返回是否因为超出上限被截断。"""
        stack = [path.rstrip("/") or "/"]
        while stack:
            current = stack.pop()
            try:
                entries = self.source.list_dir(current)
            except Exception as exc:
                self.engine.log(f"展开目录失败：{current} —— {exc}", "warn")
                continue
            for entry in entries:
                child = (current.rstrip("/") or "") + "/" + entry.name
                if entry.is_dir:
                    stack.append(child)
                    continue
                if len(out) >= self.MAX_PICK_FILES:
                    return True
                out.append(self._entry(self._rel_of(child), entry.name, entry.id, entry.size, part_mb))
        return False

    def _resolve_path(self, path: str):
        """把 /a/b/c 解析成条目：只列一次父目录，不递归。"""
        from .adapters.base import RemoteItem

        clean = "/" + (path or "").strip().strip("/")
        if clean == "/":
            return RemoteItem(id="", name="", is_dir=True, path="/")
        parent, _, name = clean.rpartition("/")
        try:
            entries = self.source.list_dir(parent or "/")
        except Exception:
            # 父目录都不存在（或没权限）：按「路径没找到」处理，不要整批失败
            return None
        for entry in entries:
            if entry.name == name:
                entry.path = clean
                return entry
        return None

    def _commit(self, entries: list, truncated: bool) -> dict:
        if not entries:
            raise RuntimeError("没有可加入的文件（空文件夹，或勾选的全是目录且里面没文件）")
        stats = self.db.upsert_plan(entries)
        rels = [e["rel_path"] for e in entries]
        self.db.select_by_paths(rels, True)
        self.db.mark_picked(rels)
        self.engine.log(f"加入迁移清单：{len(entries)} 个文件")
        message = f"已加入 {len(entries)} 个文件"
        if truncated:
            message += f"（超过 {self.MAX_PICK_FILES} 个上限，只取了前 {self.MAX_PICK_FILES} 个；剩下的建议用「扫描整个目录」）"
        return {
            "ok": True,
            "added": stats,
            "count": len(entries),
            "truncated": truncated,
            "message": message,
            "selection": self.db.selection(),
        }

    def pick_files(self, items: list) -> dict:
        """把用户在目录浏览器里勾选的条目加入迁移清单。

        勾文件就只加那个文件；勾文件夹会递归展开成里面的文件。
        「迁移源目录」只是浏览的起点，加什么以勾选为准 —— 不必为了挑几个文件
        去扫整个网盘，也不用被迫整目录照搬。
        """
        self.ensure_adapters()
        if not self.source:
            raise RuntimeError(self.errors.get("source") or "源端未就绪")
        part_mb = self._part_mb()
        entries: list[dict] = []
        truncated = False
        for item in items or []:
            path = (item.get("path") or "").strip()
            if not path:
                continue
            if item.get("is_dir"):
                truncated = self._expand_dir(path, part_mb, entries) or truncated
                continue
            rel = self._rel_of(path)
            entries.append(
                self._entry(
                    rel,
                    item.get("name") or rel.rsplit("/", 1)[-1],
                    item.get("id"),
                    int(item.get("size") or 0),
                    part_mb,
                )
            )
        return self._commit(entries, truncated)

    def pick_paths(self, paths: list) -> dict:
        """按路径批量加入（粘贴一串路径的场景）。目录同样会递归展开。"""
        self.ensure_adapters()
        if not self.source:
            raise RuntimeError(self.errors.get("source") or "源端未就绪")
        part_mb = self._part_mb()
        entries: list[dict] = []
        missing: list[str] = []
        truncated = False
        for raw in paths or []:
            clean = (raw or "").strip()
            if not clean:
                continue
            found = self._resolve_path(clean)
            if found is None:
                missing.append(clean)
                continue
            if found.is_dir:
                truncated = self._expand_dir(found.path, part_mb, entries) or truncated
                continue
            entries.append(
                self._entry(self._rel_of(found.path), found.name, found.id, found.size, part_mb)
            )
        result = self._commit(entries, truncated) if entries else {
            "ok": True, "added": {"added": 0}, "count": 0, "truncated": False,
            "message": "没有找到可加入的路径", "selection": self.db.selection(),
        }
        if missing:
            shown = "、".join(missing[:3]) + ("…" if len(missing) > 3 else "")
            result["missing"] = missing
            result["message"] += f"；{len(missing)} 个路径没找到：{shown}"
        return result

    def remove_tasks(self, ids: list) -> dict:
        """从清单里移除（只删任务记录，不动网盘上的文件）。ids 为空则移除全部。"""
        if self.engine.running:
            raise RuntimeError("迁移进行中，先暂停再移除")
        if ids:
            changed = self.db.delete_tasks([int(i) for i in ids])
        else:
            before = self.db.selection().get("total", 0)
            self.db.clear()
            changed = before
        if changed:
            self.engine.log(f"从清单移除 {changed} 项")
        return {"ok": True, "changed": changed, "selection": self.db.selection()}

    def stop_transfer(self) -> dict:
        """停止当前迁移，并取消所有勾选。

        和「暂停」的区别：暂停只是停下，勾选和队列都留着，点开始就接着传；
        停止是「这批先不传了」，停稳之后把勾选清掉，下次点开始不会又跑起来。
        已传进度和任务记录全部保留，随时可以重新勾选继续。
        """
        was_running = self.engine.running
        if was_running:
            self.engine.pause()
            # 等当前分片收尾（有了读取中断机制，通常 1 秒内就停）
            deadline = time.time() + 10
            while self.engine.running and time.time() < deadline:
                time.sleep(0.1)
        changed = self.db.set_selected_all(False, False)
        # 清掉暂停标志：否则从「暂停」直接点停止后，界面会一直显示「已暂停」
        self.engine.cancel_pause()
        if was_running or changed:
            self.engine.log(f"已停止迁移，并取消 {changed} 项勾选", "warn")
        return {
            "ok": True,
            "stopped": was_running,
            "cleared": changed,
            "selection": self.db.selection(),
        }

    def retry_failed(self, ids: list | None = None) -> dict:
        """重新排队失败/中断的任务。分片进度保留，只补没传完的部分。"""
        if self.engine.running:
            raise RuntimeError("迁移进行中，先暂停再重试")
        changed = self.db.requeue([int(i) for i in ids] if ids else None)
        if changed:
            self.engine.log(f"重新排队 {changed} 个任务（已传分片保留）")
        return {"ok": True, "changed": changed, "selection": self.db.selection()}

    def report_csv(self) -> str:
        """导出迁移报告。带 BOM，Excel 双击打开中文不乱码。"""
        import csv
        import io

        state_label = {
            "pending": "待传",
            "running": "传输中",
            "done": "完成",
            "skipped": "跳过",
            "failed": "失败",
            "paused": "已暂停",
        }
        buf = io.StringIO()
        writer = csv.writer(buf, lineterminator="\r\n")
        writer.writerow(
            [
                "文件路径",
                "大小(字节)",
                "状态",
                "是否勾选",
                "已传(字节)",
                "分片数",
                "已完成分片",
                "累计尝试",
                "错误",
                "更新时间",
            ]
        )
        for row in self.db.report_rows():
            writer.writerow(
                [
                    row["rel_path"],
                    row["size"],
                    state_label.get(row["state"], row["state"]),
                    "是" if row["selected"] else "否",
                    row["done_bytes"],
                    row["total_parts"],
                    row["done_parts_count"],
                    row["attempts"],
                    (row["error"] or "").replace("\r", " ").replace("\n", " "),
                    row["updated_at"],
                ]
            )
        return buf.getvalue()

    def select_tasks(self, payload: dict) -> dict:
        """勾选/取消勾选要迁移的文件。all 为真时批量操作。"""
        if payload.get("all") is not None:
            changed = self.db.set_selected_all(
                bool(payload.get("all")), bool(payload.get("only_open"))
            )
        else:
            ids = payload.get("ids") or []
            changed = self.db.set_selected(
                [int(i) for i in ids], bool(payload.get("selected", True))
            )
        return {"ok": True, "changed": changed, "selection": self.db.selection()}

    def mkdir(self, role: str, path: str) -> dict:
        self.ensure_adapters()
        if role == "source":
            raise RuntimeError("源端只读，不需要新建文件夹；请在目标端新建")
        if not self.sink:
            raise RuntimeError(self.errors.get("target") or "目标端未就绪")
        path = (path or "").strip()
        if not path.startswith("/"):
            raise RuntimeError("目录必须以 / 开头")
        file_id = self.sink.ensure_dir(path)
        return {"ok": True, "path": path, "file_id": file_id}

    def test_side(self, role: str) -> dict:
        self.ensure_adapters()
        if role == "source":
            if not self.source:
                raise RuntimeError(self.errors.get("source") or "源端未就绪")
            entries = self.source.list_dir(self.cfg["source"].get("root_path") or "/")
        else:
            if not self.sink:
                raise RuntimeError(self.errors.get("target") or "目标端未就绪")
            entries = self.sink.browse(self.cfg["target"].get("root_path") or "/")
        return {"ok": True, "count": len(entries)}

    def auth_adapter(self, role: str):
        """拿到一个能做 OAuth 的对象（源/目标都可能需要）。"""
        cfg = self.cfg["source" if role == "source" else "target"]
        provider = cfg.get("provider") or "mock"
        if provider == "baidu":
            from .adapters.baidu import BaiduSource

            return BaiduSource(cfg)
        if provider == "aliyun":
            from .adapters.aliyun import AliyunSink

            return AliyunSink(cfg)
        raise RuntimeError(f"{registry.get(provider)['name']} 不需要也不支持授权")

    def _target_quota(self) -> dict:
        """目标盘容量，60 秒缓存 —— 前端每 1.2 秒轮询一次，没必要每次都打网盘接口。"""
        now = time.time()
        cached = self._quota_cache
        if cached["data"] is not None and now - cached["at"] < 60:
            return cached["data"]

        data: dict = {"provider": (self.cfg.get("target") or {}).get("provider") or ""}
        if self.sink is None:
            data["error"] = self.errors.get("target") or "目标端未就绪"
        else:
            getter = getattr(self.sink, "quota", None)
            info = None
            try:
                info = getter() if callable(getter) else None
            except Exception as exc:
                data["error"] = str(exc)
            if info:
                data.update(info)
            elif "error" not in data:
                data["error"] = "该网盘没有提供容量查询"
        self._quota_cache = {"at": now, "data": data}
        return data

    # ---------------- 状态 ----------------
    def state(self) -> dict:
        self.ensure_adapters()
        snapshot = self.engine.snapshot(task_limit=700)
        tasks = snapshot.pop("tasks", [])
        # 扫描可能不是这个页面发起的（比如上一个标签页或接口触发），
        # 所以界面要靠服务端状态决定要不要显示"停止扫描"
        snapshot["scan_running"] = bool(
            self._scan_thread and self._scan_thread.is_alive()
        )
        describe = {}
        for role, adapter in (("source", self.source), ("target", self.sink)):
            if adapter is None:
                describe[role] = {"error": self.errors.get(role) or "未就绪"}
                continue
            try:
                describe[role] = adapter.describe()
            except Exception as exc:
                describe[role] = {"error": str(exc)}
        return {
            "config": config.masked(self.cfg),
            # 界面只给「真能用的网盘」，本地演示这类测试装置不下发
            "providers": registry.list_providers(include_hidden=False),
            "errors": self.errors,
            "describe": describe,
            "engine": snapshot,
            "tasks": tasks,
            "selection": self.db.selection(),
            "schedule": self.scheduler.status(),
            "quota": self._target_quota(),
            # 登录浏览器：界面据此决定「Safari」这一项要不要显示、能不能选
            "login": self.login_capability(),
            "version": __version__,
            "started_at": self.started_at,
            "pid": self.pid,
        }

    # ---------------- 动作 ----------------
    def scan(self) -> dict:
        self.ensure_adapters()
        if not self.engine.source or not self.engine.sink:
            raise RuntimeError(self.errors.get("source") or self.errors.get("target") or "两端未就绪")
        return self.engine.scan()

    def start(self) -> dict:
        self.ensure_adapters()
        return self.engine.start()

    def scan_async(self) -> dict:
        self.ensure_adapters()
        if self.engine.snapshot(1)["running"]:
            raise RuntimeError("迁移进行中，先暂停再扫描")
        if self._scan_thread and self._scan_thread.is_alive():
            return {"started": False, "msg": "正在扫描中"}
        self._scan_error = ""

        def run():
            try:
                self._scan_result = self.scan()
                self._scan_error = ""
            except Exception as exc:
                self._scan_error = str(exc)
                self.engine.log(f"扫描失败：{exc}", "error")

        self._scan_thread = threading.Thread(target=run, name="scan", daemon=True)
        self._scan_thread.start()
        return {"started": True}

    def scan_status(self) -> dict:
        return {
            "running": bool(self._scan_thread and self._scan_thread.is_alive()),
            "phase": self.engine.scan_state(),
            "error": self._scan_error,
            "result": self._scan_result,
        }

    def _scheduled_job(self) -> None:
        try:
            if self.engine.snapshot(1)["running"]:
                return
            self.scan()
            self.engine.start()
            self.engine.log("定时任务已触发", "info")
        except Exception as exc:
            self.engine.log(f"定时任务失败：{exc}", "error")

    def browse(self, role: str, path: str) -> dict:
        self.ensure_adapters()
        path = path or "/"
        if role == "source":
            if not self.source:
                raise RuntimeError(self.errors.get("source") or "源端未就绪")
            entries = self.source.list_dir(path)
        else:
            if not self.sink:
                raise RuntimeError(self.errors.get("target") or "目标端未就绪")
            entries = self.sink.browse(path)
        entries.sort(key=lambda e: (not e.is_dir, e.name.lower()))
        return {"path": path, "entries": [e.to_dict() for e in entries]}

    def oauth_start(self, role: str, redirect_uri: str) -> dict:
        adapter = self.auth_adapter(role)
        if hasattr(adapter, "authorize_url"):
            url = adapter.authorize_url(redirect_uri)
            return {"url": url}
        raise RuntimeError("该网盘不支持 OAuth 授权")

    def oauth_exchange(self, role: str, code: str, redirect_uri: str) -> dict:
        adapter = self.auth_adapter(role)
        tokens = adapter.exchange_code(code, redirect_uri)
        self._save_tokens(role, tokens)
        return {"ok": True, "expires_in": tokens.get("expires_in")}

    def _save_tokens(self, role: str, tokens: dict) -> None:
        side = "source" if role == "source" else "target"
        patch = {
            side: {
                "access_token": tokens.get("access_token", ""),
                "refresh_token": tokens.get("refresh_token", ""),
                "expires_at": tokens.get("expires_at", 0),
            }
        }
        self.cfg = config.update(patch)
        config.snapshot_side(self.cfg, side)
        self.cfg = config.save(self.cfg)
        self._stamp = ""
        self.ensure_adapters()

    def update_config(self, patch: dict) -> dict:
        allowed = {"source", "target", "transfer", "schedule", "login"}
        clean = {k: v for k, v in (patch or {}).items() if k in allowed}
        # 迁移进行中不允许改「源/目标」的网盘、目录等结构性配置，否则引擎手里的
        # 适配器会和界面显示脱节（用户看到的是新网盘，实际还在传旧网盘）。
        # transfer/schedule 这类运行时参数可以在跑的时候改，不受此限。
        if self.engine.running:
            for side in ("source", "target"):
                block = clean.get(side)
                if isinstance(block, dict) and block:
                    raise RuntimeError("迁移进行中，先暂停再切换网盘或目录")
        # 迁移中改 transfer 参数（并发/限速/分片大小）要在下一轮生效前不打断当前分片，
        # 这里仅做提示性透传，引擎 worker 读取 settings 时自然拿到新值。

        if "schedule" in clean:
            sched = clean["schedule"] or {}
            self.scheduler.configure(sched.get("enabled"), sched.get("cron"))

        # 切换网盘时，先把当前的凭据归档到 accounts，再换上新网盘的凭据。
        # 不这么做的话，同一个 cookie/token 字段会互相覆盖 —— 从百度切到天翼，
        # 百度的登录态就没了，切回来还得重新扫码。
        for side in ("source", "target"):
            block = clean.get(side)
            if not isinstance(block, dict):
                continue
            old = (self.cfg.get(side) or {}).get("provider")
            new = (block.get("provider") or old or "").strip()
            if not old or not new or old == new:
                continue
            config.snapshot_side(self.cfg, side)
            self.cfg = config.save(self.cfg)
            merged = dict(self.cfg.get(side) or {})
            merged.update(block)
            merged["provider"] = new
            self.cfg[side] = merged
            config.restore_side(self.cfg, side)
            self.cfg = config.save(self.cfg)
            clean.pop(side, None)

        self.cfg = config.update(clean)
        self._stamp = ""
        self.ensure_adapters()
        return {"ok": True}

    def swap_sides(self) -> dict:
        """把源/目标两端配置整体对调（网盘、认证方式、目录、凭据全部互换）。"""
        if self.engine.running:
            raise RuntimeError("迁移进行中，先暂停再交换源与目标")
        self.cfg = config.load()
        src = dict(self.cfg.get("source") or {})
        dst = dict(self.cfg.get("target") or {})
        # 两端直接互换。roles 语义是「角色」不是「网盘」，所以 provider 等字段
        # 原样对调即可，不需要关心 source/sink 能力是否匹配 —— 那由前端下拉和
        # ensure_adapters 兜底。
        self.cfg["source"] = dst
        self.cfg["target"] = src
        self.cfg = config.save(self.cfg)
        self._stamp = ""
        self.ensure_adapters()
        # 交换后旧的迁移清单语义已反（从 A→B 变成 B→A），不清掉会导致用户
        # 看到上一网盘的文件还挂在清单里、目录也对不上。这里清空并提示。
        cleared = 0
        if self.db.selection().get("total", 0):
            cleared = self.db.selection()["total"]
            self.db.clear()
        if cleared:
            self.engine.log(f"交换源与目标，已清空 {cleared} 项旧迁移清单", "warn")
        return {"ok": True, "source": src.get("provider"), "target": dst.get("provider"), "cleared": cleared}


class Handler(BaseHTTPRequestHandler):
    server_version = "panmigrate/0.1"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # ---------- 基础 ----------
    def log_message(self, fmt, *args):  # 静音默认访问日志
        pass

    def _send(self, status: int, payload: bytes, content_type: str, extra: dict | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(payload)))
        self.send_header("Cache-Control", "no-store")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        try:
            self.wfile.write(payload)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, data, status: int = 200) -> None:
        body = json.dumps(data, ensure_ascii=False, default=str).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8")

    def _fail(self, message: str, status: int = 400) -> None:
        self._json({"ok": False, "error": message}, status)

    def _read_json(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ValueError("请求体过大")
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8"))
        except (json.JSONDecodeError, UnicodeDecodeError):
            return {}

    def _static(self, name: str) -> None:
        target = (WEB_DIR / name).resolve()
        if not str(target).startswith(str(WEB_DIR.resolve())) or not target.is_file():
            self._send(404, b"not found", "text/plain; charset=utf-8")
            return
        ctype = mimetypes.guess_type(str(target))[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        self._send(200, target.read_bytes(), ctype)

    # ---------- GET ----------
    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        route = parsed.path
        try:
            if route in ("/", "/index.html"):
                return self._static("index.html")
            if route in ("/app.js", "/style.css", "/theme-dark.css", "/theme-light.css"):
                return self._static(route.lstrip("/"))
            # 品牌 logo 等静态素材（web/logo/ 目录）
            if route.startswith("/logo/") and route.endswith((".svg", ".png", ".jpg", ".jpeg")):
                return self._static(route.lstrip("/"))
            if route == "/api/state":
                return self._json(self.app.state())
            if route == "/api/tasks":
                return self._json({"tasks": self.app.db.list_tasks(700)})
            if route == "/api/qr/debug":
                return self._json(self.app.qr_debug())
            if route == "/api/report.csv":
                body = ("\ufeff" + self.app.report_csv()).encode("utf-8")
                name = time.strftime("panmigrate-report-%Y%m%d-%H%M.csv")
                return self._send(
                    200,
                    body,
                    "text/csv; charset=utf-8",
                    {"Content-Disposition": f'attachment; filename="{name}"'},
                )
            if route == "/api/oauth/callback":
                return self._oauth_callback(parsed)
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:
            self._fail(f"{type(exc).__name__}: {exc}", 500)

    def _oauth_callback(self, parsed):
        query = urllib.parse.parse_qs(parsed.query)
        code = (query.get("code") or [""])[0]
        role = (query.get("state") or ["source"])[0]
        if not code:
            return self._html("授权失败", "回调里没有拿到 code，请回到界面手动粘贴。")
        try:
            self.app.oauth_exchange(role, code, self._callback_url(role))
            self._html("授权成功", "凭据已保存，可以关闭这个页面回到工具了。")
        except Exception as exc:
            self._html("授权失败", str(exc))

    def _callback_url(self, role: str) -> str:
        host = self.headers.get("Host") or "127.0.0.1:8899"
        return f"http://{host}/api/oauth/callback?state={role}"

    def _html(self, title: str, message: str) -> None:
        page = f"""<!doctype html><meta charset="utf-8"><title>{title}</title>
<body style="font-family:system-ui,-apple-system,'Segoe UI',sans-serif;padding:48px;max-width:640px;margin:0 auto;color:#1f2328">
<h2 style="font-size:18px;font-weight:600">{title}</h2>
<p style="color:#57606a;line-height:1.7">{message}</p></body>"""
        self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")

    # ---------- POST ----------
    def do_POST(self):
        route = urllib.parse.urlparse(self.path).path
        try:
            payload = self._read_json()
            if route == "/api/config":
                return self._json(self.app.update_config(payload))
            if route == "/api/swap":
                return self._json(self.app.swap_sides())
            if route == "/api/tylogin/start":
                return self._json(self.app.tylogin_start(payload.get("role") or "target"))
            if route == "/api/tylogin/status":
                return self._json(self.app.tylogin_status())
            if route == "/api/tylogin/cancel":
                return self._json(self.app.tylogin_cancel())
            if route == "/api/web-login/start":
                return self._json(
                    self.app.weblogin_start(
                        payload.get("role") or "target", payload.get("site") or ""
                    )
                )
            if route == "/api/web-login/status":
                return self._json(self.app.weblogin_status())
            if route == "/api/web-login/cancel":
                return self._json(self.app.weblogin_cancel())
            if route == "/api/browse":
                return self._json(
                    self.app.browse(payload.get("role") or "source", payload.get("path") or "/")
                )
            if route == "/api/scan":
                return self._json(self.app.scan_async())
            if route == "/api/scan_status":
                return self._json(self.app.scan_status())
            if route == "/api/scan_state":
                return self._json(self.app.engine.scan_state())
            if route == "/api/start":
                return self._json(self.app.start())
            if route == "/api/pause":
                return self._json(self.app.engine.pause())
            if route == "/api/reset":
                return self._json(self.app.engine.reset())
            if route == "/api/stop":
                return self._json(self.app.stop_transfer())
            if route == "/api/qr/start":
                return self._json(self.app.qr_start(payload.get("role") or "source"))
            if route == "/api/qr/poll":
                return self._json(self.app.qr_poll(payload.get("sid") or ""))
            if route == "/api/pick":
                return self._json(self.app.pick_files(payload.get("items") or []))
            if route == "/api/pick_paths":
                return self._json(self.app.pick_paths(payload.get("paths") or []))
            if route == "/api/remove":
                return self._json(self.app.remove_tasks(payload.get("ids") or []))
            if route == "/api/scan_cancel":
                return self._json(self.app.engine.cancel_scan())
            if route == "/api/select":
                return self._json(self.app.select_tasks(payload))
            if route == "/api/retry":
                return self._json(self.app.retry_failed(payload.get("ids") or None))
            if route == "/api/mkdir":
                return self._json(
                    self.app.mkdir(payload.get("role") or "target", payload.get("path") or "")
                )
            if route == "/api/test":
                return self._json(self.app.test_side(payload.get("role") or "source"))
            if route == "/api/oauth/start":
                return self._json(
                    self.app.oauth_start(
                        payload.get("role") or "source",
                        payload.get("redirect_uri") or self._callback_url(payload.get("role") or "source"),
                    )
                )
            if route == "/api/oauth/exchange":
                return self._json(
                    self.app.oauth_exchange(
                        payload.get("role") or "source",
                        payload.get("code") or "",
                        payload.get("redirect_uri") or self._callback_url(payload.get("role") or "source"),
                    )
                )
            if route == "/api/schedule":
                return self._json(
                    self.app.update_config({"schedule": payload})
                )
            if route == "/api/quit":
                # 双击启动的 .app 没有终端可按 Ctrl+C，必须给界面一个正常退出的口子。
                # 迁移中不允许退出，避免把正在传的分片留在半路。
                if self.app.engine.snapshot(1)["running"]:
                    self._fail("迁移进行中，先暂停或停止再退出", 400)
                    return
                self._json({"ok": True, "msg": "正在退出"})
                # shutdown() 会阻塞到 serve_forever 返回，不能在处理线程里直接调
                threading.Timer(0.2, self.server.shutdown).start()
                return
            if route == "/api/demo":
                return self._json(self.app.run_demo())
            self._send(404, b"not found", "text/plain; charset=utf-8")
        except Exception as exc:
            detail = str(exc) or type(exc).__name__
            # 完整 traceback 落盘（data/panmigrate.log），用户「检查日志」时能定位到
            # 具体是哪一行出的错，而不是只有一个笼统的报错文案。
            tb = traceback.format_exc()
            self.app.engine.log(f"接口 {route} 出错：{detail}", "error")
            try:
                self.app.engine.log(f"  详情：{tb}", "error")
            except Exception:
                pass
            self._fail(detail, 400)


def _is_our_server(host: str, port: int) -> dict | None:
    """探测这个端口上跑的是不是本程序（靠 /api/state 的返回结构判断）。

    比"看进程名"可靠得多：进程名在中文/打包环境下编码容易乱，
    而接口返回里同时有 version + engine + selection，基本不可能是别的服务。
    """
    try:
        import urllib.request
        url = f"http://{host}:{port}/api/state"
        with urllib.request.urlopen(url, timeout=3) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if isinstance(data, dict) and "version" in data and "engine" in data:
            return data
    except Exception:
        pass
    return None


def _reclaim_port(host: str, port: int) -> int | None:
    """端口被占用时，如果占用者就是本程序的旧实例，结束它，让新实例接管同一地址。

    为什么必须这样：以前端口被占就静默换端口 + 开新标签，
    浏览器里会攒下一堆「搬山」标签，用户切回最早那个看到的还是旧界面
    （"改了没生效"，其实是老进程还在服务）。杀掉旧实例、沿用同一端口后，
    浏览器打开的还是同一个 URL，刷新即新版。

    安全前提：先用 /api/state 确认对方是本程序，再动 PID；不是本程序一律不动。
    """
    info = _is_our_server(host, port)
    if info is None:
        return None
    print(f"  端口 {port} 上是旧实例 v{info.get('version')}（进程 {info.get('pid')}，"
          f"启动于 {info.get('started_at')}），正在结束它…")
    try:
        if os.name == "nt":
            out = subprocess.run(["netstat", "-ano"], capture_output=True,
                                 text=True, errors="ignore", timeout=8).stdout
            pids = set()
            for line in out.splitlines():
                if "LISTENING" not in line.upper():
                    continue
                parts = line.split()
                if len(parts) >= 5 and parts[1].endswith(f":{port}"):
                    pids.add(parts[-1])
            for pid in pids:
                subprocess.run(["taskkill", "/F", "/PID", pid],
                               capture_output=True, timeout=8)
                print(f"  已结束旧实例（PID {pid}）")
                return int(pid)
        else:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}"],
                                 capture_output=True, text=True, timeout=8).stdout
            for pid in [x.strip() for x in out.splitlines() if x.strip()]:
                subprocess.run(["kill", "-9", pid], capture_output=True, timeout=8)
                print(f"  已结束旧实例（PID {pid}）")
                return int(pid)
    except Exception as exc:
        print(f"  （结束旧实例失败：{exc}）")
    return None


class _Server(ThreadingHTTPServer):
    """Windows 上必须关掉 allow_reuse_address。

    socketserver 默认开启 SO_REUSEADDR，而 Windows 的语义是「允许两个进程
    绑同一端口」—— 实测旧实例占着 8899 时，新实例照样能 bind 成功，
    于是两个进程同时服务同一端口，请求随机落到旧的那个，
    表现为「exe 换了但界面还是旧的」。关掉后第二次绑定会失败，
    才能走到「结束旧实例、接管端口」的分支。
    macOS/Linux 保留复用，避免 TIME_WAIT 导致切不回来。
    """
    allow_reuse_address = os.name != "nt"
    daemon_threads = True


def serve(host: str = "127.0.0.1", port: int = 8899) -> None:
    # 行缓冲：控制台和重定向到文件时都能即时看到启动信息（否则排查时全是空白）
    try:
        sys.stdout.reconfigure(line_buffering=True)
    except Exception:
        pass
    app = App()
    # 先看这个端口上是不是已经跑着一个「我们」：是就先结束它，
    # 这样浏览器打开的还是同一个地址，刷新就是新版，不会又攒一个标签。
    existing = _is_our_server(host, port)
    if existing:
        busy_with_work = bool((existing.get("engine") or {}).get("running"))
        if busy_with_work:
            # 旧实例正在传文件：绝不能为了「换新版」把传输打断。
            # 让它继续跑，自己换端口启动，并在控制台说清楚。
            print("  检测到旧实例正在迁移，为避免中断传输，本次改用新端口启动。")
            print("  （要切到新版，请等这轮传完再双击，或在旧窗口按 Ctrl+C 停止）")
        else:
            _reclaim_port(host, port)
            time.sleep(0.8)
    try:
        httpd = _Server((host, port), Handler)
    except OSError:
        # 先尝试接管：如果占端口的是本程序的旧实例，结束它，沿用同一个地址，
        # 这样浏览器里那个旧标签刷新一下就是新版，不会又开一个。
        killed = _reclaim_port(host, port)
        if killed:
            time.sleep(1.0)
        try:
            httpd = _Server((host, port), Handler)
        except OSError:
            # 占端口的不是自己（或者结束失败），才退到换端口
            busy = port
            httpd = _Server((host, 0), Handler)
            port = httpd.server_address[1]
            print(f"  端口 {host}:{busy} 被其他程序占用，改用 {port} 启动")
    httpd.app = app  # type: ignore[attr-defined]
    url = f"http://{host}:{httpd.server_address[1]}"
    print(f"  panmigrate v{__version__} 已启动  ->  {url}")
    print(f"  数据目录：{config.DATA_DIR}")
    print(f"  进程号：{os.getpid()}  启动于 {app.started_at}")
    print("  提示：如果浏览器里看到的是旧界面，请关掉所有旧的「搬山」标签页，")
    print("        并确认右上角版本号是 v%s。" % __version__)
    print("  按 Ctrl+C 停止")
    _open_browser(url)
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n  已停止")
    finally:
        httpd.server_close()


def _open_browser(url: str) -> None:
    """启动后自动用系统默认浏览器打开界面。打包后双击 exe/app 即用，无需手动输网址。

    默认开启，可用环境变量 PANMIGRATE_NO_BROWSER=1 关掉（命令行/无头环境）。
    """
    if os.environ.get("PANMIGRATE_NO_BROWSER"):
        return
    try:
        import webbrowser
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    except Exception:
        pass
