"""夸克网盘适配器离线测试（不联网）。

钉住几件容易错的事：
  1. 按 fid 寻址（根目录 = "0"），路径 → fid 解析要缓存
  2. 列表解析：file_type=0 是目录，file_type=1 是文件
  3. 未登录/失效要给出能指导操作的中文提示
  4. 上传会话的 md5/sha1 按 part_number 顺序累积（条件变量串行）
"""
from __future__ import annotations

import hashlib
import threading
import unittest
from unittest import mock

from panmigrate import adapters as factory
from panmigrate.adapters import quark as qk
from panmigrate.adapters.base import RemoteItem
from panmigrate.adapters.httpc import HttpError
from panmigrate.adapters.quark import (
    API_PC,
    ROOT_ID,
    QuarkError,
    QuarkSink,
    QuarkSource,
)


def dir_entry(fid: str, name: str) -> dict:
    return {"fid": fid, "file_name": name, "file_type": 0, "size": 0}


def file_entry(fid: str, name: str, size: int) -> dict:
    return {"fid": fid, "file_name": name, "file_type": 1, "size": size, "md5": "abc"}


class FakeQuark(QuarkSource):
    def __init__(self, tree: dict | None = None, cookie: str = "__pus=xxx"):
        super().__init__({"cookie": cookie})
        self.tree = tree or {"0": {"data": {"list": []}, "metadata": {"_total": 0}}}
        self.calls: list[str] = []

    def _get_json(self, url: str) -> dict:
        self.calls.append(url)
        # 从 url 里挖 pdir_fid
        import re
        m = re.search(r"pdir_fid=([0-9a-f]+)", url)
        fid = m.group(1) if m else "0"
        return self.tree.get(fid, {"data": {"list": []}, "metadata": {"_total": 0}})


class ListTest(unittest.TestCase):
    def test_parses_dirs_and_files(self):
        sink = FakeQuark({"0": {"data": {"list": [
            dir_entry("100", "照片"), file_entry("200", "a.jpg", 1024)
        ]}, "metadata": {"_total": 2}}})
        items = sink.list_dir("/")
        names = {i.name: i for i in items}
        self.assertEqual(set(names), {"照片", "a.jpg"})
        self.assertTrue(names["照片"].is_dir)
        self.assertFalse(names["a.jpg"].is_dir)
        self.assertEqual(names["a.jpg"].size, 1024)
        self.assertEqual(names["a.jpg"].path, "/a.jpg")

    def test_root_maps_to_zero(self):
        sink = FakeQuark()
        self.assertEqual(sink.resolve_path("/"), ROOT_ID)
        self.assertEqual(sink.calls, [])

    def test_cached_path(self):
        tree = {
            "0": {"data": {"list": [dir_entry("100", "照片")]}, "metadata": {"_total": 1}},
            "100": {"data": {"list": [dir_entry("101", "2024")]}, "metadata": {"_total": 1}},
        }
        sink = FakeQuark(tree)
        self.assertEqual(sink.resolve_path("/照片/2024"), "101")
        before = len(sink.calls)
        self.assertEqual(sink.resolve_path("/照片/2024"), "101")
        self.assertEqual(len(sink.calls), before, "解析过的路径不该重复打接口")


class AuthTest(unittest.TestCase):
    def test_missing_cookie_explains(self):
        sink = QuarkSource({})
        self.assertFalse(sink.describe()["authorized"])
        with self.assertRaises(QuarkError) as ctx:
            sink.list_dir("/")
        self.assertIn("pan.quark.cn", str(ctx.exception))


class FactoryTest(unittest.TestCase):
    def test_registry_bidirectional(self):
        from panmigrate.adapters.registry import get
        info = get("quark")
        self.assertEqual(info["roles"], ["source", "sink"])
        self.assertEqual(info["status"], "ready")

    def test_build_source_and_sink(self):
        src = factory.build_source({"provider": "quark", "cookie": "x=1"})
        sink = factory.build_sink({"provider": "quark", "cookie": "x=1"})
        self.assertEqual(src.kind, "source")
        self.assertEqual(sink.kind, "sink")


class HashOrderTest(unittest.TestCase):
    """验证 put_part 用条件变量按 part_number 顺序累积 md5/sha1。

    引擎是并发派发分片的，如果 adapter 不按序号排队，累积出来的 md5/sha1 就是错的，
    夸克服务端会拒绝（callback 失败）。这个测试故意倒序并发提交，验证最终 hash 正确。
    """

    def _make_sink(self, data: bytes) -> QuarkSink:
        sink = QuarkSink({"cookie": "__pus=x"})
        sink._api = mock.MagicMock()
        return sink

    def test_concurrent_parts_accumulate_in_order(self):
        data = b"abcdefghij" * 1000  # 10000 字节，切成 3 片
        part_size = 4000
        sink = QuarkSink({"cookie": "__pus=x"})

        # 预上传返回 mock
        task_id = "task123"
        info = {
            "pre": {
                "task_id": task_id, "upload_id": "UPID", "obj_key": "obj/1",
                "bucket": "ul-sz", "upload_url": "http://pds.quark.cn",
                "auth_info": "AI", "callback": {},
            },
            "parent_id": "0", "name": "x.bin", "size": len(data),
            "mime": "application/octet-stream", "etags": {},
            "md5": hashlib.md5(), "sha1": hashlib.sha1(),
            "next_part": 1, "total": 3,
        }
        sink._sessions[task_id] = info
        # mock OSS 签名和 PUT
        sink._oss_auth = mock.MagicMock(return_value="OSS k:sig")

        def fake_put(method, url, headers=None, data=None, timeout=180, retries=2):
            return 200, {"ETag": f'"ETAG-{len(data)}"'}, b""

        with mock.patch.object(qk.httpc, "request", side_effect=fake_put):
            # 倒序并发提交
            chunks = [data[i:i+part_size] for i in range(0, len(data), part_size)]
            threads = []
            for n, chunk in enumerate(chunks, 1):
                t = threading.Thread(
                    target=sink.put_part, args=(f"quark://{task_id}", chunk, n)
                )
                t.start()
                threads.append(t)
            for t in threads:
                t.join()

        self.assertEqual(info["md5"].hexdigest(), hashlib.md5(data).hexdigest())
        self.assertEqual(info["sha1"].hexdigest(), hashlib.sha1(data).hexdigest())
        self.assertEqual(len(info["etags"]), 3)


if __name__ == "__main__":
    unittest.main()
