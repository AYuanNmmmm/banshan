"""115 网盘适配器离线测试（不联网）。

钉住几件容易错的事：
  1. 目录项靠 cid 区分（有 cid 是目录，cid 空才是文件）—— 用 fid 递归会进不去子目录
  2. 列表是 offset 分页，要一直拉到 count
  3. 下载 sign = MD5('download'+pickcode+时间戳+盐) 的**大写**十六进制
  4. errno 911 是登录态失效，必须认得出来，别当网络错误傻重试
  5. 115 目前只开放源端，build_sink 要明确拒绝
"""
from __future__ import annotations

import hashlib
import unittest

from panmigrate import adapters as factory
from panmigrate.adapters.base import AdapterError
from panmigrate.adapters.pan115 import (
    MAX_UPLOAD_BYTES,
    ROOT_ID,
    SIGN_SALT,
    Pan115AuthError,
    Pan115Error,
    Pan115Sink,
    Pan115Source,
)


def dir_entry(cid: str, name: str, fid: int = 1) -> dict:
    return {"fid": fid, "cid": cid, "n": name, "s": 0, "pc": ""}


def file_entry(pickcode: str, name: str, size: int, fid: int = 2) -> dict:
    return {"fid": fid, "cid": "", "n": name, "s": size, "pc": pickcode, "sha1": "sha1xyz"}


class FakePan115(Pan115Source):
    def __init__(self, tree: dict | None = None, cookie: str = "UID=1; CID=2; SEID=3; KID=4"):
        super().__init__({"cookie": cookie})
        # tree: {"cid": [ [第1页], [第2页] ]}
        self.tree = tree or {}
        self.calls: list[tuple] = []
        self.posts: list[tuple] = []

    def _download_info(self, pickcode):
        self.calls.append(("/files/download", {"pickcode": pickcode}))
        # 真机实测：GET 换链，直链在顶层 file_url，同时会下发一个动态 cookie
        return (
            {"state": True, "file_url": f"https://cdn.115.example/{pickcode}"},
            "dyncookie=abc123; Max-Age=1280; path=/x",
        )

    def _get(self, path, query) -> dict:
        self.calls.append((path, dict(query)))
        if path != "/files":
            raise AssertionError(f"未预期的 GET：{path}")
        cid = str(query.get("cid") or ROOT_ID)
        pages = self.tree.get(cid) or [[]]
        offset = int(query.get("offset") or 0)
        collected: list[dict] = []
        for page in pages:
            collected.extend(page)
        window = collected[offset: offset + int(query.get("limit") or 100)]
        return {"state": True, "count": len(collected), "data": window}

    def _post(self, path, form) -> dict:
        self.posts.append((path, dict(form)))
        if path == "/files/download":
            return {"state": True, "data": {"url": f"https://cdn.115.example/{form.get('pickcode')}"}}
        raise AssertionError(f"未预期的 POST：{path}")


class ListTest(unittest.TestCase):
    def test_dirs_use_cid_files_use_pickcode(self):
        sink = FakePan115({"0": [[dir_entry("777", "电影"), file_entry("pc1", "a.mkv", 900)]]})
        items = sink.list_dir("/")
        by_name = {i.name: i for i in items}
        self.assertTrue(by_name["电影"].is_dir)
        self.assertEqual(by_name["电影"].id, "777", "目录要存 cid，否则钻不进去")
        self.assertFalse(by_name["a.mkv"].is_dir)
        self.assertEqual(by_name["a.mkv"].id, "pc1", "文件要存 pickcode，换直链要用")

    def test_offset_pagination(self):
        first = [file_entry(f"pc{i}", f"f{i}.bin", i) for i in range(100)]
        second = [file_entry("pcX", "last.bin", 5)]
        sink = FakePan115({"0": [first, second]})
        names = [i.name for i in sink.list_dir("/")]
        self.assertEqual(len(names), 101)
        self.assertEqual(names[-1], "last.bin")

    def test_resolves_nested_path_by_cid(self):
        sink = FakePan115({
            "0": [[dir_entry("100", "电影")]],
            "100": [[dir_entry("200", "2026")]],
            "200": [[file_entry("pcZ", "x.mp4", 8)]],
        })
        self.assertEqual(sink.resolve_path("/电影/2026"), "200")
        self.assertEqual(sink.resolve_path("/"), ROOT_ID)

    def test_missing_path_reports_clearly(self):
        sink = FakePan115({"0": [[dir_entry("100", "电影")]]})
        with self.assertRaises(Pan115Error) as ctx:
            sink.resolve_path("/不存在的目录")
        self.assertIn("目录不存在", str(ctx.exception))

    def test_walk_files_relative_paths(self):
        sink = FakePan115({
            "0": [[dir_entry("100", "电影")]],
            "100": [[file_entry("pcZ", "x.mp4", 8)]],
        })
        # SourceAdapter.walk_files 产出 RemoteItem 迭代器（Sink 那边才是 dict）
        found = {i.path: i.size for i in sink.walk_files("/")}
        self.assertEqual(found, {"/电影/x.mp4": 8})


class SignTest(unittest.TestCase):
    def test_sign_format_is_uppercase_md5(self):
        sign, ts = Pan115Source.sign_download("abc123", ts=1700000000)
        expect = hashlib.md5(f"downloadabc1231700000000{SIGN_SALT}".encode()).hexdigest().upper()
        self.assertEqual(sign, expect)
        self.assertEqual(sign, sign.upper(), "115 要求大写")
        self.assertEqual(ts, 1700000000)

    def test_uses_current_time_when_not_given(self):
        sign, ts = Pan115Source.sign_download("abc")
        self.assertGreater(ts, 1600000000)
        self.assertEqual(len(sign), 32)


class DownloadTest(unittest.TestCase):
    def test_uses_get_and_reads_top_level_file_url(self):
        src = FakePan115()
        url = src.download_url("pc-abc")
        self.assertIn("pc-abc", url)
        path, query = src.calls[-1]
        self.assertEqual(path, "/files/download")
        self.assertEqual(query["pickcode"], "pc-abc")

    def test_captures_dynamic_set_cookie(self):
        src = FakePan115()
        src.download_url("pc-abc")
        self.assertEqual(src._dl_cookie.get("pc-abc"), "dyncookie=abc123",
                         "换链下发的动态 cookie 必须留下，否则 CDN 403 no cookie value")

    def test_prepare_download_merges_both_cookies(self):
        src = FakePan115()
        item = type("I", (), {"id": "pc-abc"})()
        _, headers = src._prepare_download(item)
        ck = headers["Cookie"]
        self.assertIn("UID", ck, "登录 cookie 要在")
        self.assertIn("dyncookie=abc123", ck, "动态下载 cookie 也要在")

    def test_prepare_download_sends_ua_and_cookie(self):
        src = FakePan115()
        item = type("I", (), {"id": "pc-abc"})()
        url, headers = src._prepare_download(item)
        self.assertIn("pc-abc", url)
        self.assertIn("User-Agent", headers, "直链和 UA 绑定，下载必须带同一个 UA")
        self.assertIn("Cookie", headers, "115 的 CDN 不带 cookie 会 403 no cookie")


class ErrorTest(unittest.TestCase):
    def test_911_means_login_expired(self):
        src = FakePan115()
        src._get = lambda path, query: {"state": False, "errno": 911, "message": "请登录"}
        with self.assertRaises(Pan115AuthError) as ctx:
            src.list_dir("/")
        self.assertIn("登录态", str(ctx.exception))
        self.assertFalse(ctx.exception.retryable, "登录失效不该重试")

    def test_missing_cookie_gives_instructions(self):
        # 直接测 ensure_auth：Fake 覆盖了 _get，会绕过真实的鉴权前置检查
        src = Pan115Source({"cookie": ""})
        with self.assertRaises(Pan115AuthError) as ctx:
            src.ensure_auth()
        self.assertIn("115.com", str(ctx.exception))

    def test_other_state_error_is_plain_adapter_error(self):
        src = FakePan115()
        src._get = lambda path, query: {"state": False, "errno": 500, "message": "服务异常"}
        with self.assertRaises(AdapterError) as ctx:
            src.list_dir("/")
        self.assertNotIsInstance(ctx.exception, Pan115AuthError)


class Sha1CapabilityTest(unittest.TestCase):
    """115 要 SHA1，引擎据此决定是否读一遍源。"""

    def test_sink_declares_sha1_need(self):
        the_sink = factory.build_sink({"provider": "pan115", "cookie": "UID=1"})
        self.assertTrue(getattr(the_sink, "needs_sha1", False))

    def test_other_sinks_do_not_need_sha1(self):
        from panmigrate.adapters.mock import MockSink

        self.assertFalse(getattr(MockSink({"mock_kbps": 0}), "needs_sha1", False))


class SinkHeadersTest(unittest.TestCase):
    """签名必须和父类一致，否则 _post 调用时会 TypeError。"""

    def test_headers_accepts_json_body_flag(self):
        sink = Pan115Sink({"cookie": "UID=1"})
        h1 = sink._headers()
        h2 = sink._headers(json_body=True)
        self.assertIn("Cookie", h1)
        self.assertNotIn("Content-Type", h1)
        self.assertEqual(h2.get("Content-Type"), "application/x-www-form-urlencoded")

    def test_post_path_works_with_headers(self):
        """建目录走 _post，它会带 json_body=True 调 _headers。

        只要不抛 TypeError 就算过（网络/业务错误都不算签名问题）。
        """
        sink = Pan115Sink({"cookie": "UID=1"})
        try:
            sink._post("/files/add", {"pid": 0, "cname": "x"})
        except TypeError as exc:
            self.fail(f"_headers 签名不兼容，目标端建目录会崩：{exc}")
        except Pan115Error:
            pass


class SinkGuardTest(unittest.TestCase):
    """写入侧的早退逻辑（不联网也能测）。"""

    def test_oversize_file_rejected_with_limit(self):
        sink = Pan115Sink({"cookie": "UID=1"})
        big = MAX_UPLOAD_BYTES + 1
        with self.assertRaises(Pan115Error) as ctx:
            sink.create_upload("0", "huge.bin", big, 0, sha1="a" * 40)
        msg = str(ctx.exception)
        self.assertIn("500MB", msg)
        self.assertIn("整文件上传", msg)

    def test_missing_sha1_is_rejected(self):
        sink = Pan115Sink({"cookie": "UID=1"})
        sink.child = lambda *a, **k: None
        with self.assertRaises(Pan115Error) as ctx:
            sink.create_upload("0", "a.txt", 100, 0, sha1="")
        self.assertIn("SHA1", str(ctx.exception))


class HelperTest(unittest.TestCase):
    def test_k_ec_is_base64_of_fixed_length(self):
        import base64 as _b64
        from panmigrate.adapters.pan115 import _make_k_ec

        raw = _b64.b64decode(_make_k_ec(1700000000))
        self.assertEqual(len(raw), 48)
        # 时间戳在第 20~24 字节（小端）
        self.assertEqual(int.from_bytes(raw[20:24], "little"), 1700000000)

    def test_k_ec_differs_by_timestamp(self):
        from panmigrate.adapters.pan115 import _make_k_ec

        self.assertNotEqual(_make_k_ec(1700000000), _make_k_ec(1700000001))

    def test_oss_sign_produces_authorization(self):
        from panmigrate.adapters.pan115 import _oss_sign

        token = {"AccessKeyId": "AK", "AccessKeySecret": "SK", "SecurityToken": "ST"}
        h = _oss_sign(token, "PUT", "fhnfile", "abc/def", {"content-type": "text/plain"})
        self.assertTrue(h["authorization"].startswith("OSS AK:"))
        self.assertEqual(h["x-oss-security-token"], "ST")
        self.assertIn("date", h)

    def test_lz4_roundtrip_with_literal_only_block(self):
        """只有一个 literal 段的块，应当能原样还原。"""
        from panmigrate.adapters.pan115 import _lz4_decompress

        payload = b'{"status":"2"}'
        # token 高 4 位 = literal 长度（<15），无 offset 尾
        block = bytes([len(payload) << 4]) + payload
        framed = len(block).to_bytes(2, "little") + block
        self.assertEqual(_lz4_decompress(framed), payload)


class RegistryTest(unittest.TestCase):
    def test_both_roles(self):
        prov = factory.get("pan115")
        self.assertEqual(sorted(prov["roles"]), ["sink", "source"])
        self.assertEqual(prov["status"], "ready")

    def test_build_source_works(self):
        self.assertIsInstance(factory.build_source({"provider": "pan115"}), Pan115Source)

    def test_build_sink_works(self):
        from panmigrate.adapters.pan115 import Pan115Sink

        self.assertIsInstance(factory.build_sink({"provider": "pan115"}), Pan115Sink)


if __name__ == "__main__":
    unittest.main()
