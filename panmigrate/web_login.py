"""通用「扫码登录」：拉起受控浏览器 → 用户扫码 → 自动取登录态 → 关浏览器。

和 tylogin.py（天翼）是同一套路子，区别在于天翼只要 cookie，而这两家存的地方不同：

  115  → cookie 里（UID / CID / SEID / KID），标准 cookie 鉴权
  123  → localStorage 里（authorToken，JWT），请求时放 `Authorization: Bearer`

为什么不让用户自己抓：
  123 的 token 在 localStorage、115 的 cookie 有十几个字段，手抄一遍又长又容易漏，
  而且两者都会过期。让浏览器自己登录、我们从活着的进程里取，是最省事也最可靠的方式。

铁律：**拿到东西不等于登录成功**。两家对匿名访客都会下发跟踪用的 cookie/ID，
所以必须再调一次真实接口确认（115 看 state，123 看 code），否则用户会以为登上了，
一操作就报未登录。

全程纯标准库（cdp.py），不依赖 node。
"""
from __future__ import annotations

import os
import json
import re
import subprocess
import threading
import time
from pathlib import Path

from . import cdp

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

SITES: dict[str, dict] = {
    "pan115": {
        "label": "115 网盘",
        # mode=wangpan 会直接进网盘；未登录时页面自己会弹登录框（含扫码）
        "start_url": "https://115.com/?mode=wangpan",
        "mode": "cookie",
        # 这四个齐了才算真正登录：CID/SEID/KID 是会话身份，UID 是用户 ID
        "required_cookies": ["UID", "CID", "SEID", "KID"],
        "domain_re": re.compile(r"(\.115\.com|115\.com)$"),
        "verify_url": (
            "https://webapi.115.com/files?aid=1&cid=0&o=user_ptime&asc=0&offset=0"
            "&show_dir=1&limit=1&snap=0&natsort=1&record_open_time=1&source="
            "&format=json&star=0&stem=0&suffix="
        ),
    },
    "pan123": {
        "label": "123 云盘",
        # 直接开登录页：官网首页是营销落地页，不保证自动跳转，用户会看到「没有二维码」
        "start_url": (
            "https://user.123pan.cn/centerlogin"
            "?redirect_url=https%3A%2F%2Fyun.123pan.cn%2F"
        ),
        "mode": "token",
        # token 存在网盘页的 localStorage 里，所以要盯这个域
        "token_key": "authorToken",
        "token_origin": "yun.123pan.cn",
        "verify_url": "https://yun.123pan.cn/api/user/info",
    },
}


class WebLoginError(RuntimeError):
    pass


def _http_json(url: str, headers: dict) -> dict:
    from .adapters import httpc  # 延迟导入，避免和 adapters 包互相拖累

    return httpc.get_json(url, headers=headers, retries=0)


def verify_pan115(cookie: str) -> tuple[bool, str]:
    """用 cookie 列一次目录：state=true 才算真的登录了。"""
    if not cookie:
        return False, "没拿到任何 cookie"
    try:
        data = _http_json(SITES["pan115"]["verify_url"], {
            "Cookie": cookie, "User-Agent": UA, "Referer": "https://115.com/",
            "Accept": "application/json, text/plain, */*",
        })
    except Exception as exc:  # noqa: BLE001
        return False, f"校验登录态时请求失败：{exc}"
    if data.get("state") is True or data.get("state") == 1:
        # 115 的列目录响应里带 user_id，正好当账号标识显示
        uid = str(data.get("user_id") or data.get("uid") or "").strip()
        return True, uid or "已登录"
    errno = data.get("errno") or data.get("errcode") or ""
    if str(errno) in ("911", "401", "105"):
        return False, f"登录态无效（errno {errno}）"
    return False, f"校验未通过（errno {errno} {data.get('message') or ''}）".strip()


def verify_pan123(token: str) -> tuple[bool, str]:
    """用 token 查一次账号信息：code=0 才算真的登录了。"""
    if not token:
        return False, "没拿到 token"
    try:
        data = _http_json(SITES["pan123"]["verify_url"], {
            "Authorization": f"Bearer {token}",
            "platform": "web",
            "App-Version": "132",
            "User-Agent": UA,
            "Referer": "https://yun.123pan.cn/",
            "Accept": "application/json, text/plain, */*",
        })
    except Exception as exc:  # noqa: BLE001
        return False, f"校验登录态时请求失败：{exc}"
    if data.get("code") in (0, "0"):
        info = data.get("data") or {}
        name = str(info.get("Nickname") or info.get("Passport") or info.get("UID") or "").strip()
        return True, name or "已登录"
    return False, f"校验未通过（code {data.get('code')} {data.get('message') or ''}）".strip()


VERIFIERS = {"pan115": verify_pan115, "pan123": verify_pan123}


class WebLogin:
    """跑一轮「开浏览器 → 等扫码 → 取登录态 → 关浏览器」。"""

    def __init__(
        self,
        site: str,
        edge: str,
        profile: Path,
        port: int,
        timeout: float = 300.0,
        poll: float = 1.0,
        backend: str = "cdp",
    ):
        if site not in SITES:
            raise WebLoginError(f"不支持的扫码登录站点：{site}")
        self.site = site
        self.cfg = SITES[site]
        self.edge = edge
        self.profile = Path(profile)
        self.port = int(port)
        self.timeout = timeout
        self.poll = poll
        self.backend = backend
        self.proc: subprocess.Popen | None = None
        self._safari = None
        self._log_path = self.profile.parent / f"weblogin-{site}-browser.log"
        self._log_file = None
        self._dead_polls = 0
        self._stop = threading.Event()
        self._last_reject = ""
        # 登录成功后拿到的账号（脱敏昵称/UID），界面显示「已登录 xxx」
        self.account = ""

    # ---------- 浏览器 ----------
    def _launch(self) -> None:
        # Safari 走系统自带 safaridriver（WebDriver），没有 CDP、也没有独立 profile
        if self.backend == "safari":
            from . import safari

            self._safari = safari.SafariSession(window=(1100, 900))
            self._safari.start(self.cfg["start_url"])
            return
        # 复用同一个 profile：第二次点「扫码登录」时浏览器还是登录态，几乎瞬间完成。
        # 要换账号就在窗口里退出重登。
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [
            self.edge,
            f"--remote-debugging-port={self.port}",
            "--remote-allow-origins=*",  # 不开这个，CDP 的 WebSocket 会 403
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--window-size=1100,900",
            self.cfg["start_url"],
        ]
        # 浏览器的 stderr 落盘：它起不来时（沙箱被拦、GPU 崩、被安全软件掐掉）
        # 真正的死因在这里，比一句「窗口已关闭」有用
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self._log_path, "wb")
        except OSError:
            self._log_file = None
        self.proc = subprocess.Popen(
            args, stdout=subprocess.DEVNULL, stderr=self._log_file or subprocess.DEVNULL
        )

    def _port_alive(self) -> bool:
        """调试端口是否还活着（比 proc.poll() 可靠，理由见 tylogin 同名方法）。"""
        try:
            cdp.http_json(self.port, "/json/version", timeout=1.5)
            return True
        except Exception:
            return False

    def crash_hint(self, lines: int = 2) -> str:
        if self._log_file is not None:
            try:
                self._log_file.flush()
            except Exception:
                pass
        try:
            raw = self._log_path.read_bytes()[-8192:].decode("utf-8", "replace")
        except OSError:
            return ""
        picked = []
        for line in raw.splitlines():
            low = line.lower()
            if any(k in low for k in ("fatal", "cannot", "failed", "error", "denied", "not permitted")):
                text = line.strip()
                if "] " in text:
                    text = text.split("] ", 1)[1]
                if text and text not in picked:
                    picked.append(text)
        if not picked:
            return ""
        return f"浏览器最后报错：{' / '.join(picked[-lines:])[:220]}"

    def _pages(self) -> list[dict]:
        if self.backend == "safari":
            # Safari 没有 /json/list 这套 CDP 发现接口，也不分标签页
            return []
        try:
            return [p for p in cdp.http_json(self.port, "/json/list", timeout=2)
                    if p.get("type") == "page"]
        except Exception:
            return []

    def _wait_port(self, seconds: float = 25.0) -> bool:
        deadline = time.time() + seconds
        while time.time() < deadline:
            if self._stop.is_set():
                return False
            try:
                cdp.http_json(self.port, "/json/version", timeout=2)
                return True
            except Exception:
                time.sleep(0.4)
        return False

    def close_browser(self) -> None:
        """优先 CDP Browser.close（精确），退路才是 taskkill 指定 PID。"""
        if self.backend == "safari":
            if self._safari is not None:
                self._safari.quit()
                self._safari = None
            return
        closed = False
        try:
            ver = cdp.http_json(self.port, "/json/version", timeout=2)
            ws_url = (ver or {}).get("webSocketDebuggerUrl") or ""
            if ws_url:
                ws = cdp.WebSocket(ws_url, timeout=5)
                try:
                    ws.send('{"id":1,"method":"Browser.close"}')
                    time.sleep(0.5)
                    closed = True
                finally:
                    ws.close()
        except Exception:
            pass
        if not closed and self.proc and self.proc.poll() is None:
            try:
                if os.name == "posix":
                    # macOS / Linux 没有 taskkill，直接结束自己拉起的进程
                    self.proc.terminate()
                    self.proc.wait(timeout=8)
                else:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=10,
                    )
            except Exception:
                pass
        if self._log_file is not None:
            try:
                self._log_file.close()
            except Exception:
                pass
            self._log_file = None

    def stop(self) -> None:
        self._stop.set()

    # ---------- 取登录态 ----------
    def _cookie_state(self, pages: list[dict]) -> tuple[str, str]:
        """115：凑齐必需 cookie 才算数，返回 (cookie 串, 账号)。"""
        if self.backend == "safari":
            if self._safari is None or not self._safari.alive():
                return "", ""
            cookies = self._safari.cookies()
            jar = {c.get("name"): c.get("value") for c in cookies if c.get("value")}
            if not all(jar.get(n) for n in self.cfg["required_cookies"]):
                return "", ""
            relevant = [
                c for c in cookies
                if c.get("value") and self.cfg["domain_re"].search(c.get("domain") or "")
            ]
            if not relevant:  # 有些浏览器读回的 cookie 不带 domain，退一步全带上
                relevant = [c for c in cookies if c.get("value")]
            cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in relevant)
            return cookie_str, str(jar.get("UID") or "")
        if not pages:
            return "", ""
        ws_url = pages[0].get("webSocketDebuggerUrl") or ""
        if not ws_url:
            return "", ""
        try:
            ws = cdp.WebSocket(ws_url, timeout=10)
            try:
                result = ws.call("Storage.getCookies", {})
            finally:
                ws.close()
        except Exception:
            return "", ""
        jar = {c.get("name"): c.get("value") for c in (result.get("cookies") or []) if c.get("value")}
        if not all(jar.get(n) for n in self.cfg["required_cookies"]):
            return "", ""
        # 只带 115 相关域，别把无关 cookie 塞进请求头
        relevant = [
            c for c in (result.get("cookies") or [])
            if c.get("value") and self.cfg["domain_re"].search(c.get("domain") or "")
        ]
        cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in relevant)
        return cookie_str, str(jar.get("UID") or "")

    def _token_state(self, pages: list[dict]) -> tuple[str, str]:
        """123：token 在网盘页的 localStorage，且登录前后会跨子域，只认指定 origin。"""
        origin_needle = self.cfg["token_origin"]
        if self.backend == "safari":
            # Safari 只有一个窗口：得等页面自己跳到 yun.123pan.cn 才读得到 token
            if self._safari is None or not self._safari.alive():
                return "", ""
            if origin_needle not in self._safari.current_url():
                return "", ""
            token = self._safari.local_storage(self.cfg["token_key"])
            if token and len(token) > 40:
                return token, ""
            return "", ""
        for tab in pages:
            if origin_needle not in (tab.get("url") or ""):
                continue
            ws_url = tab.get("webSocketDebuggerUrl") or ""
            if not ws_url:
                continue
            try:
                ws = cdp.WebSocket(ws_url, timeout=10)
                try:
                    res = ws.call("Runtime.evaluate", {
                        "expression": f"JSON.stringify(localStorage.getItem({json.dumps(self.cfg['token_key'])})||'')",
                        "returnByValue": True,
                    })
                finally:
                    ws.close()
            except Exception:
                continue
            raw = (res.get("result") or {}).get("value") or '""'
            try:
                token = json.loads(raw)
            except Exception:
                token = ""
            if token and len(token) > 40:
                # 账号名由 verify_pan123 通过 /api/user/info 取，这里不重复读
                return token, ""
        return "", ""

    # ---------- 主流程 ----------
    def run(self, on_phase=None) -> dict:
        """返回 {"mode": "cookie"|"token", "value": 凭据, "account": 账号}。"""
        def phase(name: str) -> None:
            if on_phase:
                try:
                    on_phase(name)
                except Exception:
                    pass

        phase("starting")
        try:
            self._launch()
            if self.backend == "safari":
                # Safari 的会话在 _launch 里就建好了，没有调试端口要等
                phase("browser_ready")
            else:
                if not self._wait_port():
                    raise WebLoginError("浏览器调试端口没能启动（可能被安全软件拦了）")
                phase("browser_ready")
        except Exception:
            self.close_browser()
            raise

        phase("waiting_login")
        verifier = VERIFIERS[self.site]
        try:
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                if self._stop.is_set():
                    raise WebLoginError("已取消登录")
                if self.backend == "safari":
                    if self._safari is None or not self._safari.alive():
                        raise WebLoginError("Safari 窗口已关闭，登录未完成")
                elif self._port_alive():
                    self._dead_polls = 0
                else:
                    # 端口连续三次不通才算窗口没了，避免刚跳转时的短暂抖动误判
                    self._dead_polls += 1
                    if self._dead_polls >= 3:
                        hint = self.crash_hint()
                        detail = f"（{hint}）" if hint else ""
                        raise WebLoginError(f"浏览器窗口已关闭或失去响应{detail}")

                pages = self._pages()
                if self.site == "pan115":
                    value, account = self._cookie_state(pages)
                    mode = "cookie"
                else:
                    value, account = self._token_state(pages)
                    mode = "token"

                if value:
                    ok, detail = verifier(value)
                    if ok:
                        self.account = detail or account
                        phase("done")
                        return {"mode": mode, "value": value, "account": self.account}
                    # 有东西但还没真正登录：别急着收下，继续等
                    self._last_reject = detail

                time.sleep(self.poll)
            tail = f"（最后一次校验：{self._last_reject}）" if self._last_reject else ""
            raise WebLoginError(
                f"等待扫码超时（{int(self.timeout // 60) or 1} 分钟内没有完成登录）{tail}"
            )
        finally:
            self.close_browser()
