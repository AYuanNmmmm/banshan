"""Safari 登录通道（仅 macOS）：用系统自带的 safaridriver 走 W3C WebDriver。

为什么需要它：原来的扫码登录 / 天翼一键登录只能拉起 Chrome 或 Edge，靠 CDP
读登录态。mac 上有人机器里只有 Safari（或者不想为一个小工具再装一个浏览器），
那就完全用不了网页登录。

Safari 没有 CDP，但系统自带 `safaridriver`，它实现的是 W3C WebDriver 协议：
  POST /session                     建会话（会打开一个受控 Safari 窗口）
  POST /session/{id}/url            导航
  GET  /session/{id}/cookie         读 cookie（**含 HttpOnly**，这是关键）
  POST /session/{id}/execute/sync   在页面里跑 JS（读 localStorage 用）
  DELETE /session/{id}              关窗口、结束会话

与 CDP 方案的差异（用之前要知道）：
  - 只能开一个窗口，没有「多标签页」概念，所以 123 的 token 只能等页面自己
    跳到 yun.123pan.cn 之后才能读到；
  - 没有独立 profile，每次都是干净会话，登录态不会像 Chrome 那样留在本地复用；
  - **必须先授权**：Safari → 设置 → 高级 → 显示网页开发者功能，再到「开发」
    菜单里勾「允许远程自动化」（等价于终端执行一次 `sudo safaridriver --enable`）。
    没授权时会话建不起来，这里会把原始报错翻译成上面这句话。

全程标准库，不依赖 node / selenium。
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import time
import urllib.error
import urllib.request

__all__ = ["SafariError", "SafariSession", "driver_path", "ENABLE_HINT"]

# 没授权自动化时 safaridriver 返回的原始报错里带这个短语，用它来判定
_AUTH_NEEDLE = "Allow remote automation"

ENABLE_HINT = (
    "Safari 还没允许被自动化控制。请按这个顺序开一次："
    "① Safari →「设置」→「高级」→ 勾选「显示网页开发者功能」；"
    "② 菜单栏「开发」→ 勾选「允许远程自动化」；"
    "③ 回来重试。（也可以在终端执行一次 sudo safaridriver --enable）"
)


class SafariError(RuntimeError):
    pass


def driver_path() -> str:
    """找 safaridriver：先查 PATH，再查系统里的两个已知位置。"""
    found = shutil.which("safaridriver")
    if found:
        return found
    for cand in (
        "/usr/bin/safaridriver",
        "/System/Cryptexes/App/usr/bin/safaridriver",
    ):
        if os.path.isfile(cand):
            return cand
    return ""


def available() -> bool:
    return bool(driver_path())


def _free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


class SafariSession:
    """一个受控 Safari 会话。用完必须 quit()，否则 Safari 窗口会留在桌面上。"""

    def __init__(self, timeout: float = 30.0, window: tuple[int, int] = (1100, 900)):
        self.timeout = timeout
        self.window = window
        self.port = 0
        self.session_id = ""
        self.proc: subprocess.Popen | None = None

    # ---------------- HTTP ----------------
    def _call(self, method: str, path: str, payload: dict | None = None) -> dict:
        url = f"http://127.0.0.1:{self.port}{path}"
        data = json.dumps(payload).encode("utf-8") if payload is not None else None
        req = urllib.request.Request(
            url, data=data, method=method,
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            body = exc.read().decode("utf-8", "replace")
            try:
                value = (json.loads(body) or {}).get("value") or {}
            except Exception:
                value = {}
            message = value.get("message") or body[:200] or f"HTTP {exc.code}"
            raise SafariError(self._translate(message)) from None
        except Exception as exc:  # noqa: BLE001  连接失败/超时
            raise SafariError(f"和 Safari 自动化驱动通信失败：{exc}") from None
        try:
            return json.loads(body) or {}
        except Exception:
            return {}

    @staticmethod
    def _translate(message: str) -> str:
        """把驱动返回的英文原文翻成能照着做的中文。"""
        if _AUTH_NEEDLE.lower() in message.lower():
            return ENABLE_HINT
        if "safari" in message.lower() and "not" in message.lower():
            return f"{message}（Safari 侧拒绝了这个操作）"
        return message

    # ---------------- 生命周期 ----------------
    def start(self, url: str = "") -> None:
        driver = driver_path()
        if not driver:
            raise SafariError("这台机器上没有 safaridriver，无法用 Safari 登录")
        self.port = _free_port()
        self.proc = subprocess.Popen(
            [driver, "-p", str(self.port)],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        if not self._wait_ready():
            self.quit()
            raise SafariError("Safari 自动化驱动没能启动（端口没起来）")
        result = self._call("POST", "/session", {
            "capabilities": {"alwaysMatch": {"browserName": "safari"}},
        })
        value = result.get("value") or {}
        self.session_id = value.get("sessionId") or ""
        if not self.session_id:
            self.quit()
            raise SafariError("Safari 会话创建失败（没有返回 sessionId）")
        # 窗口按家里的截图比例给足，太小的话网盘登录框会被挤变形
        try:
            self._call("POST", f"/session/{self.session_id}/window/rect",
                       {"width": self.window[0], "height": self.window[1]})
        except SafariError:
            pass
        if url:
            self.navigate(url)

    def _wait_ready(self, seconds: float = 15.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self.proc is not None and self.proc.poll() is not None:
                return False
            try:
                self._call("GET", "/status")
                return True
            except SafariError:
                time.sleep(0.3)
        return False

    def navigate(self, url: str) -> None:
        self._call("POST", f"/session/{self.session_id}/url", {"url": url})

    def current_url(self) -> str:
        try:
            return str((self._call("GET", f"/session/{self.session_id}/url").get("value") or ""))
        except SafariError:
            return ""

    def cookies(self) -> list[dict]:
        """当前页面的 cookie。WebDriver 的 cookie 接口能看到 HttpOnly，这点比
        document.cookie 强，所以优先用它；extra 再补一层 JS 读到的。"""
        jar: dict[tuple[str, str], dict] = {}
        try:
            for item in (self._call("GET", f"/session/{self.session_id}/cookie").get("value") or []):
                if isinstance(item, dict) and item.get("name"):
                    jar[(item.get("name"), item.get("domain") or "")] = item
        except SafariError:
            pass
        # document.cookie 能补上 driver 漏掉的（非 HttpOnly）字段；HttpOnly 读不到，
        # 所以是「补充」而不是「替代」。
        try:
            raw = self._call("POST", f"/session/{self.session_id}/execute/sync", {
                "script": "return document.cookie || '';", "args": [],
            }).get("value") or ""
            host = ""
            current = self.current_url()
            if "//" in current:
                host = current.split("//", 1)[1].split("/", 1)[0]
            for chunk in str(raw).split(";"):
                if "=" not in chunk:
                    continue
                name, _, val = chunk.partition("=")
                name, val = name.strip(), val.strip()
                if not name or not val:
                    continue
                if not any(k[0] == name for k in jar):
                    jar[(name, "")] = {"name": name, "value": val, "domain": host}
        except SafariError:
            pass
        return list(jar.values())

    def local_storage(self, key: str) -> str:
        """读 localStorage[key]，读不到返回空串。"""
        try:
            value = self._call("POST", f"/session/{self.session_id}/execute/sync", {
                "script": f"return window.localStorage.getItem({json.dumps(key)}) || '';",
                "args": [],
            }).get("value")
        except SafariError:
            return ""
        return str(value or "")

    def alive(self) -> bool:
        if not self.session_id:
            return False
        if self.proc is not None and self.proc.poll() is not None:
            return False
        return True

    def quit(self) -> None:
        if self.session_id:
            try:
                self._call("DELETE", f"/session/{self.session_id}")
            except SafariError:
                pass
            self.session_id = ""
        if self.proc is not None and self.proc.poll() is None:
            try:
                self.proc.terminate()
                self.proc.wait(timeout=5)
            except Exception:  # noqa: BLE001
                try:
                    self.proc.kill()
                except Exception:  # noqa: BLE001
                    pass
        self.proc = None
