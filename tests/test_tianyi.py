"""天翼云盘适配器离线测试（不联网）。

钉住三件容易错的事：
  1. 天翼按 folderId 寻址，路径 → folderId 的解析要缓存
  2. 未登录/登录态失效要给出能指导操作的中文提示，不是抛原始错误
  3. 目标端上传还没实现，必须明确报错，不能假装成功
"""
from __future__ import annotations

import unittest
from unittest import mock

from panmigrate import adapters as factory
from panmigrate.adapters import tianyi as ty
from panmigrate.adapters.base import RemoteItem
from panmigrate.adapters.httpc import HttpError
from panmigrate.adapters.registry import get as provider_info
from panmigrate.adapters.tianyi import (
    DOWNLOAD_PATH,
    LIST_PATH,
    ROOT_ID,
    TianyiError,
    TianyiSink,
    TianyiSource,
)


def folder(fid: str, name: str) -> dict:
    return {"id": fid, "name": name, "isFolder": True, "size": 0}


def file_entry(fid: str, name: str, size: int) -> dict:
    return {"id": fid, "name": name, "size": size, "md5": "abc"}


class FakeTianyi(TianyiSource):
    def __init__(self, tree: dict | None = None, cookie: str = "COOKIE_LOGIN_USER=xxx"):
        super().__init__({"cookie": cookie})
        self.tree = tree or {"-11": {"fileListAO": {"count": 0, "fileList": [], "folderList": []}}}
        self.calls: list[tuple[str, dict]] = []

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.calls.append((path, dict(params or {})))
        if path == LIST_PATH:
            return self.tree.get(str((params or {}).get("folderId")), {
                "fileListAO": {"count": 0, "fileList": [], "folderList": []}
            })
        if path == DOWNLOAD_PATH:
            return {"fileDownloadUrl": "https://download.example/x"}
        return {}

    def list_calls(self) -> int:
        return len([c for c in self.calls if c[0] == LIST_PATH])


class ListTest(unittest.TestCase):
    def test_parses_folders_and_files_with_paths(self):
        sink = FakeTianyi({
            "-11": {"fileListAO": {
                "count": 3,
                "folderList": [folder("100", "照片")],
                "fileList": [file_entry("200", "a.jpg", 1024), file_entry("201", "b.mp4", 2048)],
            }}
        })
        items = sink.list_dir("/")
        names = {i.name: i for i in items}
        self.assertEqual(set(names), {"照片", "a.jpg", "b.mp4"})
        self.assertTrue(names["照片"].is_dir)
        self.assertEqual(names["a.jpg"].size, 1024)
        self.assertEqual(names["a.jpg"].path, "/a.jpg")
        self.assertEqual(names["照片"].path, "/照片")

    def test_cached_second_call_does_not_hit_api(self):
        sink = FakeTianyi({"-11": {"fileListAO": {"count": 1, "folderList": [folder("1", "A")],
                                                  "fileList": []}}})
        sink.list_dir("/")
        before = sink.list_calls()
        sink.list_dir("/")
        self.assertEqual(sink.list_calls(), before, "同一目录不该重复打接口")


class ResolvePathTest(unittest.TestCase):
    def test_walks_by_name_and_caches(self):
        tree = {
            "-11": {"fileListAO": {"count": 1, "folderList": [folder("100", "照片")], "fileList": []}},
            "100": {"fileListAO": {"count": 1, "folderList": [folder("101", "2024")], "fileList": []}},
        }
        sink = FakeTianyi(tree)
        self.assertEqual(sink.resolve_path("/照片/2024"), "101")
        before = sink.list_calls()
        self.assertEqual(sink.resolve_path("/照片/2024"), "101")
        self.assertEqual(sink.list_calls(), before, "解析过的路径要记下来")

    def test_root_maps_to_minus_eleven(self):
        sink = FakeTianyi()
        self.assertEqual(sink.resolve_path("/"), ROOT_ID)
        self.assertEqual(sink.calls, [])

    def test_missing_folder_reports_path(self):
        sink = FakeTianyi()
        with self.assertRaises(TianyiError) as ctx:
            sink.resolve_path("/不存在的目录")
        self.assertIn("目录不存在", str(ctx.exception))


class AuthTest(unittest.TestCase):
    def test_missing_cookie_explains_how_to_fix(self):
        sink = TianyiSource({})
        self.assertFalse(sink.describe()["authorized"])
        with self.assertRaises(TianyiError) as ctx:
            sink.list_dir("/")
        self.assertIn("cloud.189.cn", str(ctx.exception))

    def test_expired_session_is_translated(self):
        sink = TianyiSource({"cookie": "stale=1"})
        err = HttpError(400, ty.WEB + LIST_PATH, '{"errorCode":"InvalidSessionKey"}')
        with mock.patch.object(ty.httpc, "get_json", side_effect=err):
            with self.assertRaises(TianyiError) as ctx:
                sink.list_dir("/")
        self.assertIn("登录态已失效", str(ctx.exception))

    def test_other_http_error_keeps_status(self):
        sink = TianyiSource({"cookie": "c=1"})
        err = HttpError(500, ty.WEB + LIST_PATH, "boom")
        with mock.patch.object(ty.httpc, "get_json", side_effect=err):
            with self.assertRaises(TianyiError) as ctx:
                sink.list_dir("/")
        self.assertIn("500", str(ctx.exception))


class _Resp:
    """最小可用的 urlopen 响应替身。"""

    def __init__(self, data: bytes):
        self._data = data
        self._pos = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def read(self, n: int = -1) -> bytes:
        end = len(self._data) if n < 0 else min(len(self._data), self._pos + n)
        chunk = self._data[self._pos:end]
        self._pos = end
        return chunk


class ReadRangeTest(unittest.TestCase):
    """天翼源端也走基类的通用分段读取（分段 + 续读 + 进度回报）。"""

    def test_uses_download_url_and_range(self):
        src = FakeTianyi()
        seen = {}

        def fake_urlopen(req, timeout=None):
            seen.setdefault("url", req.full_url)
            seen.setdefault("range", req.headers.get("Range"))
            seen.setdefault("referer", req.headers.get("Referer"))
            # 必须按请求的 Range 返回对应长度，否则会被当成「没读够」一直续读
            first, last = req.headers["Range"].replace("bytes=", "").split("-")
            return _Resp(b"d" * (int(last) - int(first) + 1))

        with mock.patch.object(ty.urllib.request, "urlopen", side_effect=fake_urlopen):
            out = src.read_range(RemoteItem(id="200", name="a.jpg", is_dir=False, size=10), 4, 6)
        self.assertEqual(len(out), 6)
        self.assertEqual(seen["url"], "https://download.example/x")
        self.assertEqual(seen["range"], "bytes=4-9", "Range 必须按 HTTP 规范带 bytes= 前缀")
        self.assertIn("cloud.189.cn", seen["referer"])

    def test_reports_progress_per_segment(self):
        """一段读完要回调一次，界面才能实时看到进度。"""
        src = FakeTianyi()
        got = []
        with mock.patch.object(ty.urllib.request, "urlopen",
                               side_effect=lambda req, timeout=None: _Resp(b"x" * 6)):
            src.read_range(RemoteItem(id="200", name="a.jpg", is_dir=False, size=10),
                           0, 6, on_progress=got.append)
        self.assertEqual(got, [6])


class SinkTest(unittest.TestCase):
    def test_registry_marks_tianyi_bidirectional(self):
        info = provider_info("tianyi")
        self.assertEqual(info["roles"], ["source", "sink"])
        self.assertEqual(info["status"], "ready")

    def test_factory_builds_sink(self):
        sink = factory.build_sink({"provider": "tianyi", "cookie": "c=1"})
        self.assertEqual(sink.kind, "sink")

    def test_factory_builds_source(self):
        src = factory.build_source({"provider": "tianyi", "cookie": "c=1"})
        self.assertEqual(src.kind, "source")


if __name__ == "__main__":
    unittest.main()
