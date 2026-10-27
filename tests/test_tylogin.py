"""天翼「一键登录」的判定逻辑测试（全离线）。

重点：**不能只看 cookie 名字**。天翼对匿名访客也会下发 COOKIE_LOGIN_USER，
而且匿名 cookie 同样能列出根目录 —— 只看名字或只调列目录都会把「还没登录」
误判成「登录成功」，用户以为登上了，一操作就报未授权。
唯一靠得住的是查账号信息接口：登录后才有 account 字段。
"""
from __future__ import annotations

import pathlib
import unittest
from unittest import mock

from panmigrate import tylogin


class VerifyCookieTest(unittest.TestCase):
    def test_account_present_means_logged_in(self):
        payload = {
            "res_code": 0, "res_message": "成功", "account": "158****4728@189.cn",
            "cloudCapacityInfo": {"totalSize": 1, "usedSize": 0, "freeSize": 1},
        }
        with mock.patch.object(tylogin, "verify_cookie", wraps=tylogin.verify_cookie):
            from panmigrate.adapters import httpc

            with mock.patch.object(httpc, "get_json", return_value=payload):
                ok, detail = tylogin.verify_cookie("COOKIE_LOGIN_USER=x")
        self.assertTrue(ok)
        self.assertIn("189.cn", detail)

    def test_no_account_means_not_logged_in(self):
        """匿名访客：res_code=0 但没有 account，必须判成未登录。"""
        from panmigrate.adapters import httpc

        payload = {"res_code": 0, "res_message": "成功"}
        with mock.patch.object(httpc, "get_json", return_value=payload):
            ok, detail = tylogin.verify_cookie("COOKIE_LOGIN_USER=x")
        self.assertFalse(ok)
        self.assertIn("还没登录", detail)

    def test_business_error_is_reported(self):
        from panmigrate.adapters import httpc

        payload = {"res_code": 50001, "res_message": "会话失效"}
        with mock.patch.object(httpc, "get_json", return_value=payload):
            ok, detail = tylogin.verify_cookie("COOKIE_LOGIN_USER=x")
        self.assertFalse(ok)
        self.assertIn("50001", detail)

    def test_http_failure_is_not_a_crash(self):
        from panmigrate.adapters import httpc

        with mock.patch.object(httpc, "get_json", side_effect=OSError("boom")):
            ok, detail = tylogin.verify_cookie("COOKIE_LOGIN_USER=x")
        self.assertFalse(ok)
        self.assertIn("失败", detail)

    def test_empty_cookie_rejected_without_request(self):
        from panmigrate.adapters import httpc

        with mock.patch.object(httpc, "get_json") as spy:
            ok, _ = tylogin.verify_cookie("")
        self.assertFalse(ok)
        spy.assert_not_called()

    def test_uses_account_endpoint_not_file_list(self):
        """回归保护：判定必须走 getUserSizeInfo。

        曾经用「列根目录」判断，结果匿名 cookie 也能列出来，
        于是没登录也显示「已登录」。
        """
        self.assertIn("getUserSizeInfo", tylogin.VERIFY_URL)
        self.assertNotIn("listFiles", tylogin.VERIFY_URL)


class BrowserLivenessTest(unittest.TestCase):
    """浏览器拉起后「还算不算活着」的判定。

    背景：macOS 上 Chromium 可能把活交给另一个进程、自己先退出，用 proc.poll()
    会把「窗口开着」误判成「已关闭」，天翼登录刚开就报失败。所以改成看调试端口。
    """

    def test_dead_port_is_not_alive(self):
        import socket

        with socket.socket() as s:          # 占一个端口再关掉，保证没人监听
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        runner = tylogin.TyLogin("edge.exe", pathlib.Path("/tmp/t/.p"), port)
        self.assertFalse(runner._port_alive())

    def test_crash_hint_extracts_reason(self):
        log = tmp = pathlib.Path("/tmp" + "/pm-tylogin-test.log")
        log.write_text(
            "noise line\n"
            "[123:456:1027/125452.659488:FATAL:gpu_data_manager_impl_private.cc:408] "
            "GPU process isn't usable. Goodbye.\n",
            encoding="utf-8",
        )
        try:
            runner = tylogin.TyLogin("edge.exe", pathlib.Path("/tmp" + "/.p"), 9)
            runner._log_path = log
            hint = runner.crash_hint()
            self.assertIn("GPU process isn't usable", hint)
            self.assertTrue(hint.startswith("浏览器最后报错"))
        finally:
            log.unlink(missing_ok=True)

    def test_stop_sets_flag_so_cancel_returns_fast(self):
        runner = tylogin.TyLogin("edge.exe", pathlib.Path("/tmp" + "/.p"), 9)
        self.assertFalse(runner._stop.is_set())
        runner.stop()
        self.assertTrue(runner._stop.is_set())


if __name__ == "__main__":
    unittest.main()
