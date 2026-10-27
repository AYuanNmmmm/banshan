"""Safari 登录通道测试（全离线，不真的开浏览器）。

分两块：
  1. 协议层：SafariSession 能不能按 W3C WebDriver 说对话 —— 用一个假的
     驱动服务（标准库 http.server）应答 /status /session /cookie /execute。
  2. 平台闸：**Windows 的行为不能被这次改动带偏**。所以专门钉住三条：
     - 默认 backend 还是 cdp（老路径）；
     - 配了 safari 在非 macOS 上要被忽略；
     - Windows 的浏览器候选清单还是原来那四条。
"""
from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest import mock

from panmigrate import safari, server, tylogin, web_login


class FakeDriver(BaseHTTPRequestHandler):
    """最小的 WebDriver 假服务：只实现我们用到的几个端点。"""

    cookies_payload: list = []
    storage: dict = {}
    auth_error: str = ""

    def log_message(self, *args):  # 静音
        pass

    def _send(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        if self.path == "/status":
            return self._send(200, {"value": {"ready": True, "message": "fake"}})
        if self.path.endswith("/cookie"):
            return self._send(200, {"value": self.cookies_payload})
        if self.path.endswith("/url"):
            return self._send(200, {"value": "https://yun.123pan.cn/web/main"})
        return self._send(404, {"value": {"message": "no route"}})

    def do_POST(self):
        length = int(self.headers.get("Content-Length") or 0)
        payload = json.loads(self.rfile.read(length) or b"{}")
        if self.path == "/session":
            if self.auth_error:
                return self._send(500, {"value": {"error": "session not created",
                                                  "message": self.auth_error}})
            return self._send(200, {"value": {"sessionId": "fake-session",
                                              "capabilities": {"browserName": "safari"}}})
        if self.path.endswith("/execute/sync"):
            script = payload.get("script") or ""
            if "localStorage" in script:
                key = "authorToken"
                return self._send(200, {"value": self.storage.get(key, "")})
            return self._send(200, {"value": "UID=1; CID=2; SEID=3; KID=4"})
        if self.path.endswith("/url") or self.path.endswith("/window/rect"):
            return self._send(200, {"value": None})
        return self._send(404, {"value": {"message": "no route"}})

    def do_DELETE(self):
        return self._send(200, {"value": None})


class SafariSessionTest(unittest.TestCase):
    def setUp(self):
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), FakeDriver)
        self.port = self.httpd.server_address[1]
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        FakeDriver.auth_error = ""
        FakeDriver.cookies_payload = [
            {"name": "UID", "value": "10086", "domain": ".115.com"},
            {"name": "CID", "value": "c", "domain": ".115.com"},
            {"name": "SEID", "value": "s", "domain": ".115.com"},
            {"name": "KID", "value": "k", "domain": ".115.com"},
            {"name": "other", "value": "x", "domain": ".example.com"},
        ]
        FakeDriver.storage = {"authorToken": "T" * 60}

    def tearDown(self):
        self.httpd.shutdown()
        self.httpd.server_close()

    def _session(self) -> safari.SafariSession:
        sess = safari.SafariSession(timeout=5)
        # 直接接管端口，跳过真的起 safaridriver 进程
        sess.port = self.port
        result = sess._call("POST", "/session", {"capabilities": {"alwaysMatch": {
            "browserName": "safari"}}})
        sess.session_id = result["value"]["sessionId"]
        return sess

    def test_cookies_include_httponly_entries(self):
        sess = self._session()
        jar = {c["name"]: c["value"] for c in sess.cookies()}
        for name in ("UID", "CID", "SEID", "KID"):
            self.assertEqual(jar.get(name) is None, False, f"缺 {name}")
        sess.quit()

    def test_local_storage_read(self):
        sess = self._session()
        self.assertEqual(sess.local_storage("authorToken"), "T" * 60)
        sess.quit()

    def test_missing_automation_permission_translated(self):
        """没开「允许远程自动化」时，报错必须翻成能照着做的中文步骤。"""
        FakeDriver.auth_error = (
            "Could not create a session: You must enable 'Allow remote automation' "
            "in the Developer section of Safari Settings to control Safari via WebDriver."
        )
        sess = safari.SafariSession(timeout=5)
        sess.port = self.port
        with self.assertRaises(safari.SafariError) as ctx:
            sess._call("POST", "/session", {"capabilities": {"alwaysMatch": {}}})
        self.assertIn("允许远程自动化", str(ctx.exception))
        self.assertNotIn("Allow remote automation", str(ctx.exception))


class PlatformGuardTest(unittest.TestCase):
    """Windows 侧行为必须和改动前一致。"""

    def test_windows_candidates_unchanged(self):
        with mock.patch.object(sys, "platform", "win32"):
            paths = server._chromium_candidates()
        self.assertEqual(paths, [
            r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
            r"C:\Program Files\Google\Chrome\Application\chrome.exe",
            r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        ])

    def test_safari_preference_ignored_off_macos(self):
        """配置里写了 safari，在 Windows 上也不能选 Safari。"""
        app = server.App.__new__(server.App)  # 只测选路，不跑完整初始化
        app.cfg = {"login": {"browser": "safari"}}
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(server, "_chromium_candidates", return_value=[]), \
                mock.patch.object(safari, "driver_path", return_value=""):
            self.assertEqual(app._login_browser(), ("", "", ""))

    def test_windows_prefers_chromium_even_if_configured_safari(self):
        app = server.App.__new__(server.App)
        app.cfg = {"login": {"browser": "safari"}}
        fake = r"C:\Program Files\Microsoft\Edge\Application\msedge.exe"
        with mock.patch.object(sys, "platform", "win32"), \
                mock.patch.object(server, "_chromium_candidates", return_value=[fake]), \
                mock.patch.object(server.os.path, "isfile", return_value=True):
            backend, path, label = app._login_browser()
        self.assertEqual(backend, "cdp")
        self.assertEqual(path, fake)
        self.assertEqual(label, "Edge")

    def test_login_modules_default_to_cdp(self):
        """不传 backend 时必须还是老路径，不能默认跑去 Safari。"""
        t = tylogin.TyLogin("edge.exe", "/tmp/x", 1234)
        w = web_login.WebLogin("pan115", "edge.exe", "/tmp/y", 1234)
        self.assertEqual(t.backend, "cdp")
        self.assertEqual(w.backend, "cdp")


class SafariVerifyTest(unittest.TestCase):
    def test_cookie_requires_all_four_fields(self):
        login = web_login.WebLogin("pan115", "edge.exe", "/tmp/z", 1, backend="safari")

        class FakeSafari:
            def __init__(self, cookies):
                self._cookies = cookies

            def alive(self):
                return True

            def cookies(self):
                return self._cookies

        login._safari = FakeSafari([{"name": "UID", "value": "1", "domain": ".115.com"}])
        self.assertEqual(login._cookie_state([]), ("", ""))

        login._safari = FakeSafari([
            {"name": n, "value": "v", "domain": ".115.com"}
            for n in ("UID", "CID", "SEID", "KID")
        ])
        cookie, uid = login._cookie_state([])
        self.assertEqual(uid, "v")
        self.assertIn("UID=v", cookie)
        self.assertNotIn("example.com", cookie)


if __name__ == "__main__":
    unittest.main()
