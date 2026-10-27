"""天翼云盘「一键登录」：拉起一个受控浏览器，用户登录后自动抓取登录态。

为什么必须这么绕：天翼的 cookie 是 app-bound 加密（DPAPI），从磁盘上的
Cookies 库里读出来也是密文，离线解不开；只能从活着的浏览器进程经 CDP 取。

实现用纯标准库（见 cdp.py），**不需要 node** —— 这样 exe 拷到任何一台
Windows 上都能直接用，不用先装运行时。
"""
from __future__ import annotations

import os
import re
import subprocess
import threading
import time
from pathlib import Path

from . import cdp

TARGET_URL = "https://cloud.189.cn/"
# 页面上出现这个 cookie 说明会话已经建立（注意：**不能只靠它判断登录**，
# 天翼对匿名访客也会下发同名 cookie，所以还要再调一次接口实测可用性）
LOGIN_COOKIE = "COOKIE_LOGIN_USER"
# 只保留天翼相关的域，别把无关 cookie 一起带上（请求头会变长、也可能被判异常）
DOMAIN_RE = re.compile(r"(\.189\.cn|dlife\.cn)$")
# 查账号信息：唯一能区分「匿名访客」和「真的登录了」的接口。
# 登录后它会带回 account（脱敏手机号/邮箱），匿名时没有这个字段。
# 列目录接口不行 —— 匿名 cookie 也能列出根目录，会把没登录误判成成功。
VERIFY_URL = "https://cloud.189.cn/api/portal/getUserSizeInfo.action"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


def verify_cookie(cookie: str) -> tuple[bool, str]:
    """用 cookie 查一次账号信息，确认登录态真的可用。

    返回 (是否可用, 说明)：成功时说明是账号名（如 138****0000@189.cn），
    失败时说明是原因。

    为什么不用「列目录」判断：天翼对匿名访客也会下发 COOKIE_LOGIN_USER，
    而且匿名 cookie 同样能列出根目录 —— 拿它判断会把「还没登录」当成
    「登录成功」，用户以为登上了，一操作就报未授权。
    """
    if not cookie:
        return False, "没拿到任何 cookie"
    from .adapters import httpc   # 延迟导入，避免和 adapters 包互相拖累

    try:
        data = httpc.get_json(
            VERIFY_URL,
            headers={
                "Cookie": cookie,
                "User-Agent": UA,
                "Referer": "https://cloud.189.cn/web/main/",
                "Accept": "application/json;charset=UTF-8",
            },
            retries=0,
        )
    except Exception as exc:  # noqa: BLE001
        return False, f"校验登录态时请求失败：{exc}"
    if not isinstance(data, dict):
        return False, "校验登录态时返回了非预期内容"
    code = data.get("res_code")
    if code not in (0, "0"):
        msg = data.get("res_message") or data.get("errorCode") or ""
        return False, f"登录态校验未通过（res_code={code} {msg}）".strip()
    account = str(data.get("account") or "").strip()
    if not account:
        return False, "还没登录（接口没返回账号信息）"
    return True, account


class TyLogin:
    """跑一轮「开浏览器 → 等登录 → 取 cookie → 关浏览器」。"""

    def __init__(
        self,
        edge: str,
        profile: Path,
        port: int,
        url: str = TARGET_URL,
        timeout: float = 300.0,
        poll: float = 0.8,
        backend: str = "cdp",
    ):
        self.edge = edge
        self.profile = Path(profile)
        self.port = int(port)
        self.url = url
        self.timeout = timeout
        self.poll = poll
        self.backend = backend
        self.proc: subprocess.Popen | None = None
        self._safari = None
        self._log_path = self.profile.parent / "tylogin-browser.log"
        self._log_file = None
        self._dead_polls = 0
        self._stop = threading.Event()
        self._last_reject = ""
        # 登录成功后拿到的账号（脱敏手机号/邮箱），界面上可以显示「已登录 xxx」
        self.account = ""

    # ---------- 浏览器 ----------
    def _launch(self) -> None:
        # Safari 走 WebDriver（系统自带 safaridriver），没有 CDP 也没有 profile 概念
        if self.backend == "safari":
            from . import safari

            self._safari = safari.SafariSession(window=(1000, 900))
            self._safari.start(self.url)
            return
        # 复用同一个独立 profile，**不要删**：
        #   1) 用户第一次登录后，再点「一键登录」浏览器直接就是登录态，
        #      几乎瞬间完成，不用反复扫码/输密码；
        #   2) 递归删几百个文件在某些环境会触发「批量删除」安全拦截，
        #      导致登录根本起不来；
        #   3) 万一要换账号，在浏览器窗口里退出重登即可。
        # 这个目录和用户日常用的浏览器完全隔离，不会互相影响。
        self.profile.mkdir(parents=True, exist_ok=True)
        args = [
            self.edge,
            f"--remote-debugging-port={self.port}",
            # CDP 的 WebSocket 会带 Origin 头，不开这个会被拒绝握手
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile}",
            "--no-first-run",
            "--no-default-browser-check",
            "--disable-sync",
            "--window-size=1000,900",
            self.url,
        ]
        # 浏览器自己的 stderr 落盘：它起不来时（沙箱被拦、GPU 崩了、被安全软件
        # 掐掉）真正的死因在这，比一句「窗口已关闭」有用得多。
        try:
            self._log_path.parent.mkdir(parents=True, exist_ok=True)
            self._log_file = open(self._log_path, "wb")
        except OSError:
            self._log_file = None
        self.proc = subprocess.Popen(
            args,
            stdout=subprocess.DEVNULL,
            stderr=self._log_file or subprocess.DEVNULL,
        )

    def _port_alive(self) -> bool:
        """调试端口还活着吗。

        比 `proc.poll()` 可靠：Chromium 在 macOS 上可能把活交给另一个进程、
        自己先退出，这时窗口明明还在，poll() 却已经不是 None（会误判成「已关闭」）。
        """
        try:
            cdp.http_json(self.port, "/json/version", timeout=1.5)
            return True
        except Exception:
            return False

    def crash_hint(self, lines: int = 2) -> str:
        """从浏览器日志里摘出最像死因的几行，拼成一句给用户看。"""
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
                # 日志里会带时间戳/进程号，掐掉前导符号，只留人话部分
                if "] " in text:
                    text = text.split("] ", 1)[1]
                if text and text not in picked:
                    picked.append(text)
        if not picked:
            return ""
        tail = " / ".join(picked[-lines:])
        return f"浏览器最后报错：{tail[:220]}"

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

    def _page_ws(self, seconds: float = 12.0) -> str:
        deadline = time.time() + seconds
        while time.time() < deadline:
            try:
                pages = [p for p in cdp.http_json(self.port, "/json/list", timeout=2)
                         if p.get("type") == "page"]
            except Exception:
                pages = []
            for page in pages:
                if "cloud.189.cn" in (page.get("url") or ""):
                    return page.get("webSocketDebuggerUrl") or ""
            if pages:
                return pages[0].get("webSocketDebuggerUrl") or ""
            time.sleep(0.4)
        return ""

    def close_browser(self) -> None:
        """关掉我们拉起的那个浏览器。

        优先走 CDP 的 Browser.close（精确、不会误杀用户自己开的 Edge）；
        退路才是 taskkill 指定 PID 的进程树。Safari 走 WebDriver 的结束会话。
        """
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
                    # macOS / Linux 上没有 taskkill，直接结束我们拉起的那个进程
                    self.proc.terminate()
                    self.proc.wait(timeout=8)
                else:
                    subprocess.run(
                        ["taskkill", "/F", "/T", "/PID", str(self.proc.pid)],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                        timeout=10,
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
        """用户点「取消」时调用：让轮询尽快退出并关掉浏览器。"""
        self._stop.set()

    # ---------- 主流程 ----------
    def run(self, on_phase=None) -> str:
        """返回 cookie 字符串；失败抛 RuntimeError（消息给用户看）。"""
        def phase(name: str) -> None:
            if on_phase:
                try:
                    on_phase(name)
                except Exception:
                    pass

        phase("starting")
        ws = None
        try:
            self._launch()
            if self.backend == "safari":
                # Safari 的会话在 _launch 里就建好了，不需要等调试端口
                phase("browser_ready")
            else:
                if not self._wait_port():
                    raise RuntimeError("浏览器调试端口没能启动（可能被安全软件拦了）")
                phase("browser_ready")

                ws_url = self._page_ws()
                if not ws_url:
                    raise RuntimeError("找不到可控制的浏览器页面")
                ws = cdp.WebSocket(ws_url, timeout=15)
        except Exception:
            self.close_browser()
            raise

        phase("waiting_login")
        try:
            deadline = time.time() + self.timeout
            while time.time() < deadline:
                if self._stop.is_set():
                    raise RuntimeError("已取消登录")
                # 用户把浏览器窗口关掉了：别傻等 5 分钟，立刻告诉他没登成。
                # 判定用「调试端口连续三次不通」，而不是进程状态 —— 见 _port_alive 的注释。
                if self.backend == "safari":
                    if self._safari is None or not self._safari.alive():
                        raise RuntimeError("Safari 窗口已关闭，登录未完成")
                    cookies = self._safari.cookies()
                else:
                    if self._port_alive():
                        self._dead_polls = 0
                    else:
                        self._dead_polls += 1
                        if self._dead_polls >= 3:
                            hint = self.crash_hint()
                            detail = f"（{hint}）" if hint else ""
                            raise RuntimeError(f"浏览器窗口已关闭或失去响应{detail}")
                    try:
                        result = ws.call("Storage.getCookies", {})
                    except Exception:
                        # 浏览器可能刚跳转过、连接短暂不可用，等下一轮
                        time.sleep(self.poll)
                        continue
                    cookies = result.get("cookies") or []
                hit = next((c for c in cookies
                            if c.get("name") == LOGIN_COOKIE and c.get("value")), None)
                if hit:
                    relevant = [
                        c for c in cookies
                        if c.get("value") and DOMAIN_RE.search(c.get("domain") or "")
                    ]
                    cookie_str = "; ".join(f"{c['name']}={c['value']}" for c in relevant)
                    if LOGIN_COOKIE in cookie_str:
                        ok, detail = verify_cookie(cookie_str)
                        if ok:
                            self.account = detail
                            phase("done")
                            return cookie_str
                        # 有名字但没权限（多半还没真正登录）：别急着收下，继续等
                        self._last_reject = detail
                time.sleep(self.poll)
            tail = f"（最后一次校验：{self._last_reject}）" if self._last_reject else ""
            raise RuntimeError(
                f"等待登录超时（{int(self.timeout // 60) or 1} 分钟内没有完成登录）{tail}"
            )
        finally:
            if ws is not None:
                try:
                    ws.close()
                except Exception:
                    pass
            self.close_browser()
