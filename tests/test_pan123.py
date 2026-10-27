"""123 云盘适配器离线测试（不联网）。

钉住几件实测踩出来的坑：
  1. 列表必须走 `file/list/new` —— 走废弃的 `file/list` 会永远返回空，
     这个坑会让人误以为「上传成功了但看不到文件」
  2. `Type` 字段：0 是文件、1 是目录
  3. 翻页靠 `Next`，`-1` 表示最后一页
  4. 没有 token 要直接报 AuthError 并提示扫码，不能静默当空盘
  5. 目前只开放源端，build_sink 必须明确拒绝
"""
from __future__ import annotations

import unittest

from panmigrate import adapters as factory
from panmigrate.adapters.base import AdapterError
from panmigrate.adapters.pan123 import (
    ROOT_ID,
    Pan123AuthError,
    Pan123Error,
    Pan123Sink,
    Pan123Source,
)


def dir_entry(fid: str, name: str) -> dict:
    return {"FileId": int(fid), "FileName": name, "Type": 1, "Size": 0, "Etag": ""}


def file_entry(fid: str, name: str, size: int, etag: str = "abc") -> dict:
    return {"FileId": int(fid), "FileName": name, "Type": 0, "Size": size, "Etag": etag}


class FakePan123(Pan123Source):
    """把 _call 换成内存假数据，其余逻辑走真实实现。"""

    def __init__(self, tree: dict | None = None, token: str = "tok-1234567890"):
        super().__init__({"access_token": token, "account_name": "158****4728"})
        # tree: {parentId: [ [第1页], [第2页] ]}
        self.tree = tree or {}
        self.calls: list[tuple] = []

    def _call(self, method, path, body=None, query=None):
        self.calls.append((method, path, body, query))
        if path == "/api/file/list/new":
            parent = str((query or {}).get("parentFileId") or ROOT_ID)
            pages = self.tree.get(parent) or [[]]
            cursor = str((query or {}).get("next") or "")
            idx = int(cursor) if cursor.isdigit() else 0
            page = pages[idx] if idx < len(pages) else []
            nxt = -1 if idx + 1 >= len(pages) else idx + 1
            return {
                "Next": nxt,
                "Len": len(page),
                "Total": sum(len(p) for p in pages),
                "InfoList": page,
            }
        if path == "/api/file/download_info":
            return {"DownloadUrl": f"https://dl.123pan.cn/{body.get('fileId')}"}
        raise AssertionError(f"未预期的接口：{path}")


class ListTest(unittest.TestCase):
    def test_type_zero_is_file_one_is_dir(self):
        sink = FakePan123({"0": [[dir_entry("100", "照片"), file_entry("200", "a.jpg", 1024)]]})
        items = sink.list_dir("/")
        by_name = {i.name: i for i in items}
        self.assertTrue(by_name["照片"].is_dir, "Type=1 应为目录")
        self.assertFalse(by_name["a.jpg"].is_dir, "Type=0 应为文件")
        self.assertEqual(by_name["a.jpg"].size, 1024)
        self.assertEqual(by_name["a.jpg"].md5, "abc")

    def test_uses_list_new_endpoint(self):
        sink = FakePan123({"0": [[file_entry("1", "a.txt", 1)]]})
        sink.list_dir("/")
        paths = [c[1] for c in sink.calls]
        self.assertIn("/api/file/list/new", paths)
        self.assertNotIn("/api/file/list", paths, "废弃接口返回永远是空，不能用")

    def test_follows_next_cursor(self):
        sink = FakePan123({
            "0": [[file_entry("1", "a.txt", 1)], [file_entry("2", "b.txt", 2)]],
        })
        names = [i.name for i in sink.list_dir("/")]
        self.assertEqual(names, ["a.txt", "b.txt"])

    def test_resolves_nested_path(self):
        sink = FakePan123({
            "0": [[dir_entry("100", "照片")]],
            "100": [[dir_entry("200", "2026")]],
            "200": [[file_entry("300", "x.jpg", 5)]],
        })
        self.assertEqual(sink.resolve_path("/照片/2026"), "200")
        self.assertEqual(sink.resolve_path("/"), ROOT_ID)

    def test_missing_path_reports_clearly(self):
        sink = FakePan123({"0": [[dir_entry("100", "照片")]]})
        with self.assertRaises(Pan123Error) as ctx:
            sink.resolve_path("/不存在")
        self.assertIn("目录不存在", str(ctx.exception))

    def test_walk_files_yields_paths(self):
        sink = FakePan123({
            "0": [[dir_entry("100", "照片")]],
            "100": [[file_entry("300", "x.jpg", 5)]],
        })
        found = {i.path: i.size for i in sink.walk_files("/")}
        self.assertEqual(found, {"/照片/x.jpg": 5})


class DownloadTest(unittest.TestCase):
    def _meta(self, src):
        # 换直链要五件套，这些字段由列目录时缓存下来
        src._meta["200"] = {"name": "a.jpg", "size": 1024,
                            "etag": "abc", "s3keyFlag": "4007-0"}

    def test_download_url_from_info(self):
        src = FakePan123()
        self._meta(src)
        item = type("I", (), {"id": "200"})()
        url, headers = src._prepare_download(item)
        self.assertIn("200", url)
        self.assertIn("User-Agent", headers)

    def test_sends_all_five_fields(self):
        src = FakePan123()
        self._meta(src)
        src.download_url("200")
        body = [c[2] for c in src.calls if c[1] == "/api/file/download_info"][0]
        for key in ("fileId", "size", "s3keyFlag", "fileName", "etag"):
            self.assertIn(key, body, f"换直链缺少 {key} 会被服务端拒绝")

    def test_url_is_cached_between_calls(self):
        src = FakePan123()
        self._meta(src)
        src.download_url("200")
        src.download_url("200")
        hits = [c for c in src.calls if c[1] == "/api/file/download_info"]
        self.assertEqual(len(hits), 1, "同一文件短时间内不该重复换直链")

    def test_missing_url_explains_traffic_limit(self):
        src = FakePan123()
        self._meta(src)
        src._call = lambda *a, **k: {}
        with self.assertRaises(Pan123Error) as ctx:
            src.download_url("200", force=True)
        self.assertIn("流量", str(ctx.exception))

    def test_unknown_file_asks_to_refresh(self):
        src = FakePan123()
        with self.assertRaises(Pan123Error) as ctx:
            src.download_url("999")
        self.assertIn("刷新目录", str(ctx.exception))


class AuthTest(unittest.TestCase):
    def test_no_token_tells_user_to_scan(self):
        src = Pan123Source({"access_token": ""})
        with self.assertRaises(Pan123AuthError) as ctx:
            src.list_dir("/")
        self.assertIn("扫码登录", str(ctx.exception))
        self.assertFalse(ctx.exception.retryable, "登录失效不该重试")

    def test_describe_shows_account(self):
        src = FakePan123()
        desc = src.describe()
        self.assertTrue(desc["authorized"])
        self.assertEqual(desc["account"], "158****4728")


class RegistryTest(unittest.TestCase):
    def test_both_roles_available(self):
        prov = factory.get("pan123")
        self.assertEqual(sorted(prov["roles"]), ["sink", "source"])
        self.assertEqual(prov["status"], "ready")
        self.assertEqual(prov["auth_modes"], ["web_qr"])

    def test_build_source_works(self):
        self.assertIsInstance(factory.build_source({"provider": "pan123"}), Pan123Source)

    def test_build_sink_works(self):
        from panmigrate.adapters.pan123 import Pan123Sink

        self.assertIsInstance(factory.build_sink({"provider": "pan123"}), Pan123Sink)


class FakePan123Sink(Pan123Sink):
    """模拟上传链路：预上传 → 预签名 → PUT → 合并。"""

    def __init__(self, slice_size: int = 16 * 1024 * 1024, reuse: bool = False):
        super().__init__({"access_token": "tok-1234567890"})
        self.slice_size = slice_size
        self.reuse = reuse
        self.calls: list[tuple] = []
        self.puts: list[tuple] = []
        self._seq = 100

    def _call(self, method, path, body=None, query=None):
        self.calls.append((method, path, body, query))
        if path == "/api/file/list/new":
            return {"Next": -1, "InfoList": []}
        if path == "/api/file/upload_request":
            if self.reuse:
                return {"Reuse": True, "Info": {"FileId": 999}, "Bucket": ""}
            self._seq += 1
            total = max(1, (int(body["size"]) + self.slice_size - 1) // self.slice_size)
            return {
                "Reuse": False,
                "FileId": self._seq,
                "Bucket": "123-486",
                "Key": f"k/{self._seq}",
                "UploadId": f"UP-{self._seq}",
                "StorageNode": "m8012",
                "SliceSize": str(self.slice_size),
            }
        if path in ("/api/file/s3_upload_object/auth", "/api/file/s3_repare_upload_parts_batch"):
            start = int(body["partNumberStart"])
            end = int(body["partNumberEnd"])          # 开区间
            return {"presignedUrls": {str(n): f"https://oss.example/{body['key']}?part={n}"
                                      for n in range(start, end)}, "headers": {}}
        if path == "/api/file/upload_complete/v2":
            return {"file_info": {"FileId": 777, "FileName": body["key"], "Size": body["fileSize"]}}
        raise AssertionError(f"未预期接口：{path}")

    def put_part(self, upload_url, data, part_number):
        self.puts.append((upload_url, len(data), part_number))
        return "etag-" + str(part_number)


class CreateUploadTest(unittest.TestCase):
    def test_single_part_uses_auth_endpoint(self):
        sink = FakePan123Sink()
        sess = sink.create_upload("0", "a.txt", 1000, 10 * 1024 * 1024, md5="a" * 32)
        paths = [c[1] for c in sink.calls]
        self.assertIn("/api/file/s3_upload_object/auth", paths)
        self.assertNotIn("/api/file/s3_repare_upload_parts_batch", paths)
        self.assertEqual(len(sess.parts), 1)
        self.assertEqual(sess.part_size, 16 * 1024 * 1024, "分片大小要用服务端的 SliceSize")

    def test_multi_part_uses_batch_and_sends_file_id(self):
        sink = FakePan123Sink()
        sess = sink.create_upload("0", "big.bin", 20 * 1024 * 1024, 10 * 1024 * 1024, md5="b" * 32)
        prep = [c for c in sink.calls if c[1] == "/api/file/s3_repare_upload_parts_batch"]
        self.assertEqual(len(prep), 1)
        body = prep[0][2]
        self.assertIn("fileId", body, "多分片预签名必须带 fileId")
        self.assertEqual(body["partNumberEnd"], 3, "2 片时 end 要传 3（开区间）")
        self.assertEqual(len(sess.parts), 2)

    def test_missing_md5_is_rejected_clearly(self):
        sink = FakePan123Sink()
        with self.assertRaises(Pan123Error) as ctx:
            sink.create_upload("0", "a.txt", 1000, 0, md5="")
        self.assertIn("MD5", str(ctx.exception))

    def test_reuse_hit_returns_rapid_session(self):
        sink = FakePan123Sink(reuse=True)
        sess = sink.create_upload("0", "dup.txt", 1000, 0, md5="c" * 32)
        self.assertTrue(sess.rapid)
        self.assertEqual(sess.file_id, "999")
        self.assertEqual(sess.parts, [])
        self.assertFalse([c for c in sink.calls if "s3_" in c[1]], "秒传不该再去预签名")


class CompleteTest(unittest.TestCase):
    def test_complete_sends_exact_fields(self):
        sink = FakePan123Sink()
        sess = sink.create_upload("0", "a.txt", 1000, 0, md5="d" * 32)
        fid = sink.complete(sess)
        self.assertEqual(fid, "777")
        body = [c[2] for c in sink.calls if c[1] == "/api/file/upload_complete/v2"][0]
        for key in ("fileId", "bucket", "fileSize", "key", "isMultipart", "StorageNode"):
            self.assertIn(key, body, f"合并请求必须包含 {key}")
        self.assertIs(body["isMultipart"], False)

    def test_rapid_session_completes_without_request(self):
        sink = FakePan123Sink(reuse=True)
        sess = sink.create_upload("0", "dup.txt", 1000, 0, md5="e" * 32)
        before = len(sink.calls)
        self.assertEqual(sink.complete(sess), "999")
        self.assertEqual(len(sink.calls), before, "秒传不该再发请求")


class PutPartTest(unittest.TestCase):
    def test_put_uses_presigned_url_directly(self):
        sink = FakePan123Sink()
        sess = sink.create_upload("0", "a.txt", 1000, 0, md5="f" * 32)
        etag = sink.put_part(sess.parts[0]["upload_url"], b"x" * 10, 1)
        self.assertEqual(etag, "etag-1")
        self.assertTrue(sink.puts[0][0].startswith("https://oss.example/"))


if __name__ == "__main__":
    unittest.main()
