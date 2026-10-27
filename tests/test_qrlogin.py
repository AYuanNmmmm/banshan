"""扫码登录的凭据抓取测试。

两件事必须钉住：

1. 百度扫码成功后不能只留 BDUSS/STOKEN。缺 BAIDUID 会导致下载 dlink 偶发 403
   「31326 user is not authorized」，表现为「有时能下、有时报错」。
2. 夸克换登录态**不能靠 cookie 名判断成败**。夸克改过 cookie 名，硬校验
   __pus/__puus 会把「其实已经登上了」误报成「cookie 不完整」，让用户白扫一次。
"""
from __future__ import annotations

import http.cookiejar
import json
import unittest
from unittest import mock

from panmigrate import qrlogin
from panmigrate.qrlogin import BaiduQRLogin, QuarkQRLogin, QRError


def _cookie(name: str, value: str, domain: str = ".baidu.com"):
    return http.cookiejar.Cookie(
        version=0, name=name, value=value, port=None, port_specified=False,
        domain=domain, domain_specified=True, domain_initial_dot=True,
        path="/", path_specified=True, secure=False, expires=None,
        discard=True, comment=None, comment_url=None, rest={}, rfc2109=False,
    )


class BaiduQRCookieTest(unittest.TestCase):
    def _login_with(self, cookies: list) -> dict:
        qr = BaiduQRLogin("source")
        # 模拟「手机上点了确认」：换 BDUSS 那一步直接成功
        qr.sess.get_text = lambda *a, **kw: ""
        for c in cookies:
            qr.sess.jar.set_cookie(c)
        result = qr._finish("temp_bduss")
        self.assertIsNotNone(result, "拿到 BDUSS 就该判定登录成功")
        return qr.credentials

    def test_keeps_baiduid_for_risk_control(self):
        creds = self._login_with([
            _cookie("BDUSS", "BD_VALUE"),
            _cookie("STOKEN", "ST_VALUE"),
            _cookie("BAIDUID", "BID_VALUE"),
        ])
        ck = creds["cookie"]
        self.assertIn("BDUSS=BD_VALUE", ck)
        self.assertIn("STOKEN=ST_VALUE", ck)
        self.assertIn("BAIDUID=BID_VALUE", ck, "缺 BAIDUID 会偶发 31326 下载失败")

    def test_keeps_extra_baidu_domain_cookies(self):
        """以后百度再加必需字段时，兜底逻辑要能把它们带上。"""
        creds = self._login_with([
            _cookie("BDUSS", "BD_VALUE"),
            _cookie("PANWEB", "1"),
            _cookie("SOME_NEW_TOKEN", "NEW_VALUE"),
        ])
        self.assertIn("SOME_NEW_TOKEN=NEW_VALUE", creds["cookie"])

    def test_skips_analytics_cookies(self):
        creds = self._login_with([
            _cookie("BDUSS", "BD_VALUE"),
            _cookie("H_PS_PSSID", "123_abc"),
            _cookie("BIDUPSID", "xyz"),
        ])
        # 纯统计项不往里塞，避免 cookie 头过长
        self.assertNotIn("H_PS_PSSID", creds["cookie"])
        self.assertNotIn("BIDUPSID", creds["cookie"])

    def test_requires_bduss(self):
        qr = BaiduQRLogin("source")
        qr.sess.get_text = lambda *a, **kw: ""
        # 还没确认时没有 BDUSS，应安静返回 None 让轮询继续
        self.assertIsNone(qr._finish("temp"))


class QuarkExchangeTest(unittest.TestCase):
    """夸克换登录态：以「能不能真的读网盘」为准，不看 cookie 名。"""

    def _qr_with(self, names: list[str]) -> QuarkQRLogin:
        qr = QuarkQRLogin("target")
        for n in names:
            qr.sess.jar.set_cookie(_cookie(n, "v", domain="pan.quark.cn"))
        return qr

    def test_accepts_renamed_cookies_when_api_works(self):
        """cookie 名变了（没有 __pus/__puus），但接口读得通，就该算成功。"""
        qr = self._qr_with(["ctoken", "b-user-id"])
        with mock.patch.object(qr.sess, "request", return_value=(200, {}, b'{"success":true}')), \
             mock.patch.object(qrlogin.httpc, "get_json", return_value={"code": 0}):
            cookie = qr._exchange("TICKET")
        self.assertIn("b-user-id=v", cookie)

    def test_reports_real_code_when_ticket_rejected(self):
        """接口说 ticket 无效时，要把 code/msg 带出来，而不是「cookie 不完整」。"""
        qr = self._qr_with(["ctoken"])
        body = json.dumps({
            "success": False, "code": "AUTH_ERROR:50051", "msg": "Invalid service ticket",
        }).encode()
        with mock.patch.object(qr.sess, "request", return_value=(200, {}, body)), \
             mock.patch.object(qrlogin.time, "sleep", return_value=None):
            with self.assertRaises(QRError) as ctx:
                qr._exchange("TICKET")
        msg = str(ctx.exception)
        self.assertIn("50051", msg)
        self.assertIn("Invalid service ticket", msg)
        self.assertNotIn("缺 __pus", msg, "不该再报那套误导性的文案")

    def test_reports_verify_failure_with_code(self):
        """cookie 拿到了但读不通时，要报校验的 code，并提示 cookie 名。"""
        qr = self._qr_with(["ctoken", "foo"])
        with mock.patch.object(qr.sess, "request", return_value=(200, {}, b'{"success":true}')), \
             mock.patch.object(qrlogin.httpc, "get_json",
                               return_value={"code": 31001, "message": "not login"}), \
             mock.patch.object(qrlogin.time, "sleep", return_value=None):
            with self.assertRaises(QRError) as ctx:
                qr._exchange("TICKET")
        msg = str(ctx.exception)
        self.assertIn("31001", msg)
        self.assertIn("ctoken", msg, "要把实际拿到的 cookie 名列出来，方便排查")

    def test_poll_keeps_retrying_instead_of_discarding_scan(self):
        """手机已确认但换 cookie 暂时失败时，不能立刻作废会话让用户重扫。"""
        qr = self._qr_with(["ctoken"])
        qr.token = "QR"
        poll_body = {
            "status": 2000000, "message": "ok",
            "data": {"members": {"service_ticket": "TICKET"}},
        }
        with mock.patch.object(qr.sess, "get_json", return_value=poll_body), \
             mock.patch.object(qr, "_exchange", side_effect=QRError("换登录态暂时失败")), \
             mock.patch.object(qrlogin.time, "sleep", return_value=None):
            first = qr.poll()
        self.assertEqual(first["status"], "waiting")
        self.assertFalse(first.get("done"))
        self.assertIn("第 1 次", first["message"])
        self.assertEqual(qr.confirmed_ticket, "TICKET", "要记住 ticket 供下轮复用")

    def test_poll_gives_up_after_repeated_failures(self):
        qr = self._qr_with(["ctoken"])
        qr.token = "QR"
        qr.confirmed_ticket = "TICKET"
        with mock.patch.object(qr.sess, "get_json", return_value={"status": 50004001}), \
             mock.patch.object(qr, "_exchange", side_effect=QRError("一直失败")), \
             mock.patch.object(qrlogin.time, "sleep", return_value=None):
            for _ in range(7):
                self.assertEqual(qr.poll()["status"], "waiting")
            last = qr.poll()
        self.assertEqual(last["status"], "expired")
        self.assertIn("一直失败", last["message"])


    def test_does_not_exchange_ticket_before_confirmation(self):
        """已扫码未确认（50004002）时即使带了 service_ticket 也不能去换。

        那个 ticket 还没生效，换了会被服务端拒，表现成「手机明明点了确认，
        却提示登录态不完整」。
        """
        qr = self._qr_with(["ctoken"])
        qr.token = "QR"
        # 已扫码未确认，但响应里已经带了 ticket
        poll_body = {
            "status": 50004002, "message": "scanned",
            "data": {"members": {"service_ticket": "EARLY_TICKET"}},
        }
        with mock.patch.object(qr.sess, "get_json", return_value=poll_body), \
             mock.patch.object(qr, "_exchange") as exch:
            res = qr.poll()
        exch.assert_not_called()
        self.assertEqual(res["status"], "scanned")
        self.assertEqual(res["message"], "已扫码，请在手机上点「确认登录」")
        self.assertEqual(qr.confirmed_ticket, "", "未确认的 ticket 不该被采纳")

    def test_exchanges_after_confirmed(self):
        """status=2000000 才是真的确认完成，这时才换。"""
        qr = self._qr_with(["ctoken"])
        qr.token = "QR"
        poll_body = {
            "status": 2000000, "message": "ok",
            "data": {"members": {"service_ticket": "GOOD_TICKET"}},
        }
        with mock.patch.object(qr.sess, "get_json", return_value=poll_body), \
             mock.patch.object(qr, "_exchange", return_value="ctoken=v; __pus=x") as exch:
            res = qr.poll()
        exch.assert_called_once_with("GOOD_TICKET")
        self.assertTrue(res.get("done"))
        self.assertEqual(res["status"], "confirmed")


if __name__ == "__main__":
    unittest.main()
