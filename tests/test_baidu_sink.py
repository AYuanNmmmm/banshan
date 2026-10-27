"""百度网盘写入端（BaiduSink）离线测试。

真实接口的规矩都是实测出来的，这里用假传输把它们钉住：
  1. 分片固定 4MB，和工具配置里的 part_size 无关
  2. precreate 用占位 md5，数量必须等于块数
  3. create 提交真实 md5，缺块要报错
  4. 目录/文件已存在时 api/create 会自动改名 → 必须先查存在性
  5. 0 字节文件百度不支持 → 明确报错
"""
from __future__ import annotations

import json
import unittest
from io import BytesIO as _BytesIO
from unittest import mock

from panmigrate.adapters import baidu as baidu_mod
from panmigrate.adapters.baidu import BaiduError, BaiduSink
from panmigrate.adapters.base import RemoteItem

MB = 1024 * 1024
BLOCK = 4 * MB


class FakeSink(BaiduSink):
    """拦掉所有网络调用，把请求记下来。"""

    def __init__(self, dirs: dict | None = None):
        super().__init__({"auth_mode": "qr", "cookie": "BDUSS=fake"})
        self.forms: list[tuple[str, dict]] = []
        self.blocks: list[tuple[int, int]] = []
        self._dirs = dirs or {"/": {}}

    # 目录模拟
    def list_dir(self, path: str):
        path = "/" + (path or "/").strip().strip("/")
        if path == "/":
            path = "/"
        return list(self._dirs.get(path, {}).values())

    def _post_form(self, path: str, fields: dict) -> dict:
        self.forms.append((path, dict(fields)))
        if path == "/api/precreate":
            count = len(json.loads(fields["block_list"]))
            return {"errno": 0, "uploadid": f"u{len(self.forms)}", "return_type": 1,
                    "block_list": list(range(count))}
        if path == "/api/create":
            if fields.get("isdir"):
                return {"errno": 0, "fs_id": 1}
            return {"errno": 0, "fs_id": 42}
        return {"errno": 0}

    def _upload_block(self, path, upload_id, seq, data):
        self.blocks.append((seq, len(data)))
        import hashlib
        return hashlib.md5(data).hexdigest()


class EnsureDirTest(unittest.TestCase):
    def test_existing_dir_is_not_recreated(self):
        """百度对已存在目录会「自动改名」，所以必须先查后建。"""
        sink = FakeSink({"/": {"照片": RemoteItem(id="1", name="照片", is_dir=True)}})
        sink.ensure_dir("/照片")
        self.assertEqual([f for f in sink.forms if f[1].get("isdir")], [],
                         "目录已存在却还去 create，会凭空多出一个改名目录")

    def test_missing_dir_is_created_level_by_level(self):
        sink = FakeSink()
        sink.ensure_dir("/a/b")
        creates = [f[1]["path"] for f in sink.forms if f[1].get("isdir")]
        self.assertEqual(creates, ["/a", "/a/b"])

    def test_root_returns_immediately(self):
        sink = FakeSink()
        self.assertEqual(sink.ensure_dir("/"), "/")
        self.assertEqual(sink.forms, [])


class CreateUploadTest(unittest.TestCase):
    def test_uses_4mb_blocks_regardless_of_config_part_size(self):
        sink = FakeSink()
        session = sink.create_upload("/", "大文件.bin", 9 * MB, 10 * MB)
        self.assertEqual(session.part_size, BLOCK)
        self.assertEqual(len(session.parts), 3)
        precreate = [f for f in sink.forms if f[0] == "/api/precreate"][0][1]
        self.assertEqual(len(json.loads(precreate["block_list"])), 3)
        self.assertEqual(precreate["path"], "/大文件.bin")
        self.assertEqual(precreate["size"], 9 * MB)

    def test_empty_file_is_rejected_with_reason(self):
        sink = FakeSink()
        with self.assertRaises(BaiduError) as ctx:
            sink.create_upload("/", "空.txt", 0, 8 * MB)
        self.assertIn("0 字节", str(ctx.exception))

    def test_refuse_when_name_exists(self):
        sink = FakeSink({"/": {"同名.txt": RemoteItem(id="7", name="同名.txt", is_dir=False)}})
        with self.assertRaises(BaiduError) as ctx:
            sink.create_upload("/", "同名.txt", 1024, 8 * MB, name_mode="refuse")
        self.assertIn("同名", str(ctx.exception))

    def test_auto_rename_picks_free_name(self):
        sink = FakeSink({"/": {
            "a.txt": RemoteItem(id="1", name="a.txt", is_dir=False),
            "a (1).txt": RemoteItem(id="2", name="a (1).txt", is_dir=False),
        }})
        session = sink.create_upload("/", "a.txt", 1024, 8 * MB, name_mode="auto_rename")
        self.assertEqual(session.name, "a (2).txt")
        self.assertEqual(session.parts[0]["part_number"], 1)


class CompleteTest(unittest.TestCase):
    def test_complete_sends_real_md5s(self):
        sink = FakeSink()
        session = sink.create_upload("/", "x.bin", 5 * MB, 8 * MB)
        data0, data1 = b"a" * BLOCK, b"b" * MB
        sink.put_part(session.parts[0]["upload_url"], data0, 1)
        sink.put_part(session.parts[1]["upload_url"], data1, 2)
        fs_id = sink.complete(session)
        self.assertEqual(fs_id, "42")
        create = [f for f in sink.forms if f[0] == "/api/create"][-1][1]
        import hashlib
        self.assertEqual(
            json.loads(create["block_list"]),
            [hashlib.md5(data0).hexdigest(), hashlib.md5(data1).hexdigest()],
        )
        self.assertEqual(create["uploadid"], session.upload_id)

    def test_complete_refuses_when_a_block_is_missing(self):
        sink = FakeSink()
        session = sink.create_upload("/", "x.bin", 5 * MB, 8 * MB)
        sink.put_part(session.parts[0]["upload_url"], b"a" * BLOCK, 1)
        with self.assertRaises(BaiduError) as ctx:
            sink.complete(session)
        self.assertIn("没传完", str(ctx.exception))

    def test_part_upload_requires_live_session(self):
        sink = FakeSink()
        with self.assertRaises(BaiduError):
            sink.put_part("baidu://不存在的会话", b"x", 1)


class DescribeTest(unittest.TestCase):
    def test_describe_marks_writable(self):
        sink = FakeSink()
        info = sink.describe()
        self.assertTrue(info["writable"])
        self.assertTrue(info["authorized"])


class QuotaTest(unittest.TestCase):
    def test_free_is_computed_not_taken_from_server(self):
        """百度返回的 free 实测等于 total，不能直接用。"""
        sink = FakeSink()
        payload = {"errno": 0, "total": 1000, "used": 250, "free": 1000}
        with mock.patch.object(baidu_mod.httpc, "get_json", return_value=payload):
            self.assertEqual(sink.quota(), {"total": 1000, "used": 250, "free": 750})

    def test_errno_returns_none(self):
        sink = FakeSink()
        with mock.patch.object(baidu_mod.httpc, "get_json", return_value={"errno": 2}):
            self.assertIsNone(sink.quota())


class DlinkConcurrencyTest(unittest.TestCase):
    """直链缓存必须串行解析：多分片并发时只打一次 filemetas。

    这是 31326「user is not authorized, hitcode:104」的诱因之一 ——
    缓存过期瞬间 N 个分片线程同时取链，容易被百度判异常。
    """

    def test_concurrent_dlink_hits_upstream_once(self):
        import threading
        import time

        from panmigrate.adapters.baidu import BaiduSource

        src = BaiduSource({"auth_mode": "qr", "cookie": "BDUSS=fake"})
        calls = {"n": 0}

        def fake_dlink_cookie(fs_id):
            calls["n"] += 1
            time.sleep(0.2)          # 放大并发窗口，确保线程真的撞在一起
            return "https://example.com/dlink?sign=x"

        src._dlink_cookie = fake_dlink_cookie
        item = RemoteItem(id="1", name="a.bin", is_dir=False, path="/a.bin", size=10)

        barrier = threading.Barrier(6)

        def run():
            barrier.wait()           # 6 个线程同时冲
            src.dlink(item)

        threads = [threading.Thread(target=run) for _ in range(6)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(calls["n"], 1, "并发取直链应只向上游请求一次")

    def test_force_refresh_bypasses_cache(self):
        from panmigrate.adapters.baidu import BaiduSource

        src = BaiduSource({"auth_mode": "qr", "cookie": "BDUSS=fake"})
        calls = {"n": 0}

        def fake(fs_id):
            calls["n"] += 1
            return f"https://example.com/dlink?n={calls['n']}"

        src._dlink_cookie = fake
        item = RemoteItem(id="1", name="a.bin", is_dir=False, path="/a.bin", size=10)
        first = src.dlink(item)
        second = src.dlink(item)
        self.assertEqual(first, second, "命中缓存时不该重复解析")
        third = src.dlink(item, force=True)
        self.assertNotEqual(first, third, "force=True 必须重新解析")
        self.assertEqual(calls["n"], 2)


class _FakeResp:
    """假的 urlopen 响应：可指定在第 N 个字节处断连。"""

    def __init__(self, data: bytes, fail_after: int | None = None):
        self._data = data
        self._pos = 0
        self._fail_after = fail_after

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        if self._fail_after is not None and self._pos >= self._fail_after:
            import http.client
            raise http.client.IncompleteRead(b"", 1)
        end = len(self._data) if n < 0 else min(len(self._data), self._pos + n)
        if self._fail_after is not None:
            end = min(end, self._fail_after)
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk


class ReadChunkResumeTest(unittest.TestCase):
    """连接中断要能断点续读；403 要换链重试；都用基类的通用实现。"""

    def _source(self):
        from panmigrate.adapters.baidu import BaiduSource

        src = BaiduSource({"auth_mode": "qr", "cookie": "BDUSS=fake"})
        # 直链解析统一走假数据，避免测试打真实接口
        src._prepare_download = lambda item, force_refresh=False: (
            "https://example.com/dlink", {"User-Agent": "x"})
        return src

    def test_resumes_from_break_point(self):
        """第一次只读到一半就断，第二次应从断点续读，最终数据完整。"""
        src = self._source()
        item = RemoteItem(id="1", name="a.bin", is_dir=False, path="/a.bin", size=1000)
        payload = bytes(range(256)) * 4          # 1024 字节
        ranges = []
        calls = {"n": 0}
        END = 400

        def fake_urlopen(req, timeout=None):
            rng = req.headers.get("Range")
            ranges.append(rng)
            first, last = rng.replace("bytes=", "").split("-")
            calls["n"] += 1
            if calls["n"] == 1:
                # 第一条响应只给 400 字节就「断」
                return _FakeResp(payload[:END])
            start = int(first)
            data = payload[start:start + (int(last) - start + 1)]
            return _FakeResp(data)

        with mock.patch.object(baidu_mod.urllib.request, "urlopen", side_effect=fake_urlopen), \
             mock.patch.object(baidu_mod.time, "sleep", return_value=None):
            data = src._fetch_segment(item, 0, 1024)

        self.assertEqual(data, payload, "续读后的数据必须与源完全一致")
        self.assertGreaterEqual(len(ranges), 2, "应该至少续读一次")
        self.assertTrue(ranges[0].startswith("bytes=0-"), ranges[0])
        self.assertTrue(
            any(r.startswith(f"bytes={END}-") for r in ranges[1:]),
            f"应从断点 {END} 续读，实际 {ranges}")

    def test_403_is_retried_then_gives_up_with_hint(self):
        """一直 403 时要给出可操作的提示，而不是抛裸 HTTP 错。"""
        import urllib.error

        from panmigrate.adapters.base import AdapterError

        src = self._source()
        item = RemoteItem(id="1", name="a.bin", is_dir=False, path="/a.bin", size=100)
        body = b'{"error_code":31326,"error_msg":"user is not authorized, hitcode:104"}'

        def boom(req, timeout=None):
            raise urllib.error.HTTPError(req.full_url, 403, "Forbidden", {}, _BytesIO(body))

        with mock.patch.object(baidu_mod.urllib.request, "urlopen", side_effect=boom), \
             mock.patch.object(baidu_mod.time, "sleep", return_value=None):
            with self.assertRaises(AdapterError) as ctx:
                src._fetch_segment(item, 0, 100)
        msg = str(ctx.exception)
        self.assertIn("下载失败", msg)
        self.assertIn("稳妥", msg, "要提示换稳妥档")

    def test_403_then_success_recovers(self):
        """前两次 403，第三次成功 —— 应该能扛过去。"""
        import urllib.error

        src = self._source()
        item = RemoteItem(id="1", name="a.bin", is_dir=False, path="/a.bin", size=10)
        calls = {"n": 0}

        def flaky(req, timeout=None):
            calls["n"] += 1
            if calls["n"] < 3:
                raise urllib.error.HTTPError(
                    req.full_url, 403, "Forbidden", {},
                    _BytesIO(b'{"error_code":31326,"error_msg":"x"}'))
            first, last = req.headers["Range"].replace("bytes=", "").split("-")
            return _FakeResp(b"x" * (int(last) - int(first) + 1))

        with mock.patch.object(baidu_mod.urllib.request, "urlopen", side_effect=flaky), \
             mock.patch.object(baidu_mod.time, "sleep", return_value=None):
            data = src._fetch_segment(item, 0, 10)
        self.assertEqual(len(data), 10)
        self.assertEqual(calls["n"], 3)


class ReadRangeSplitTest(unittest.TestCase):
    """百度下载单次 Range 超过 4MB 直接 403 + 31326（实测 6MB 就挂）。

    引擎按设置的分片大小（默认 10MB）取数据，源端必须自己按 4MB 切。
    """

    def _source(self):
        from panmigrate.adapters.baidu import BaiduSource

        src = BaiduSource({"auth_mode": "qr", "cookie": "BDUSS=fake"})
        src.calls = []

        def fake_segment(item, offset, length):
            assert length <= src.read_max_bytes, f"单段 {length} 超过 {src.read_max_bytes}"
            src.calls.append((offset, length))
            return b"x" * length

        src._fetch_segment = fake_segment
        return src

    def test_single_segment_not_split(self):
        MB = 1024 * 1024
        src = self._source()
        data = src.read_range(RemoteItem("1", "a", False, "/a", 20 * MB), 0, MB)
        self.assertEqual(len(data), MB)
        self.assertEqual(len(src.calls), 1)

    def test_default_10mb_is_split_into_segments(self):
        """10MB 分片按 4MB 上限切成 4+4+2。"""
        MB = 1024 * 1024
        src = self._source()
        data = src.read_range(RemoteItem("1", "a", False, "/a", 20 * MB), 0, 10 * MB)
        self.assertEqual(len(data), 10 * MB)
        self.assertEqual([n for _, n in src.calls], [4 * MB, 4 * MB, 2 * MB])
        self.assertTrue(all(n <= src.read_max_bytes for _, n in src.calls))

    def test_partial_tail_is_kept(self):
        """不足一个 4MB 段的尾巴原样保留，不能被补齐或截断。"""
        MB = 1024 * 1024
        src = self._source()
        total = int(2.5 * MB)
        data = src.read_range(RemoteItem("1", "a", False, "/a", 20 * MB), 0, total)
        self.assertEqual(len(data), total)
        self.assertEqual([n for _, n in src.calls], [total])

    def test_offset_is_continuous(self):
        MB = 1024 * 1024
        src = self._source()
        start = 3 * MB
        src.read_range(RemoteItem("1", "a", False, "/a", 40 * MB), start, 4 * MB)
        expect = start
        for offset, length in src.calls:
            self.assertEqual(offset, expect)
            expect += length
        self.assertEqual(expect, start + 4 * MB)

    def test_progress_callback_fires_per_segment(self):
        MB = 1024 * 1024
        src = self._source()
        got = []
        src.read_range(RemoteItem("1", "a", False, "/a", 20 * MB), 0, 10 * MB,
                       on_progress=got.append)
        self.assertEqual(got, [4 * MB, 4 * MB, 2 * MB], "每段读完都应回报一次进度")


if __name__ == "__main__":
    unittest.main()
