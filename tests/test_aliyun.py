"""阿里云盘适配器测试：预上传响应解析（全部离线，_post 打桩）。

覆盖真实碰到过的两种情况：
  1. 正常响应带回 upload_id + part_info_list
  2. 服务端认为文件已存在，只回 file_id，不回 upload_id（应当按秒传命中处理）
"""
from __future__ import annotations

import unittest

from panmigrate.adapters.aliyun import AliyunError, AliyunSink

MB = 1024 * 1024


def make_sink(response: dict):
    sink = AliyunSink({
        "auth_mode": "qr", "access_token": "tk", "expires_at": 0, "drive_id": "d1",
    })
    sink.ensure_drive = lambda: "d1"
    calls: list[tuple] = []

    def fake_post(path, payload, retries=3):
        calls.append((path, payload))
        return response

    sink._post = fake_post
    return sink, calls


class CreateUploadTest(unittest.TestCase):
    def test_normal_response_returns_parts(self):
        sink, calls = make_sink({
            "upload_id": "u1",
            "file_id": "f1",
            "part_info_list": [{"part_number": 1, "upload_url": "http://x/1"}],
        })
        session = sink.create_upload("pid", "a.bin", 8 * MB + 1, 8 * MB)
        self.assertFalse(session.rapid)
        self.assertEqual(session.upload_id, "u1")
        self.assertEqual(len(session.parts), 1)
        self.assertEqual(len(calls[0][1]["part_info_list"]), 2, "分片数应按 size/part_size 申请")

    def test_zero_size_file_asks_for_one_part(self):
        sink, calls = make_sink({"upload_id": "u", "file_id": "f",
                                 "part_info_list": [{"part_number": 1, "upload_url": "http://x/1"}]})
        sink.create_upload("pid", "empty.txt", 0, 8 * MB)
        self.assertEqual(len(calls[0][1]["part_info_list"]), 1)

    def test_missing_upload_id_but_file_id_is_rapid_hit(self):
        sink, _ = make_sink({"file_id": "f2", "rapid_upload": False, "parent_file_id": "pid"})
        session = sink.create_upload("pid", "a.bin", 1024, 1024)
        self.assertTrue(session.rapid, "没有 upload_id 但给了 file_id，应当按已存在处理")
        self.assertEqual(session.parts, [])
        self.assertEqual(session.file_id, "f2")

    def test_error_code_is_reported_in_chinese(self):
        sink, _ = make_sink({"code": "QuotaExhausted.Drive", "message": "no space"})
        with self.assertRaises(AliyunError) as ctx:
            sink.create_upload("pid", "a.bin", 1024, 1024)
        self.assertIn("空间不足", str(ctx.exception))

    def test_response_without_ids_is_an_error(self):
        sink, _ = make_sink({"foo": "bar"})
        with self.assertRaises(AliyunError):
            sink.create_upload("pid", "a.bin", 1024, 1024)


class QuotaTest(unittest.TestCase):
    def test_web_mode_reads_v2_drive_get(self):
        sink, calls = make_sink({"drive_id": "d1", "total_size": 100, "used_size": 40})
        self.assertEqual(sink.quota(), {"total": 100, "used": 40, "free": 60})
        self.assertEqual(calls[0][0], "/v2/drive/get", "网页态的容量接口没有 adrive 前缀")

    def test_missing_capacity_fields_returns_none(self):
        sink, _ = make_sink({"drive_id": "d1"})
        self.assertIsNone(sink.quota())


class DownloadUrlTest(unittest.TestCase):
    """下载直链必须显式带 drive_id。

    漏了会返回 400：
      "The input parameter share_id, drive_id is not valid.
       get file request drive_id and share_id are both nil"
    这个坑只在「阿里当源端」时才会踩到 —— 当目标端只走上传，不调这个接口，
    所以之前一直没暴露。
    """

    def _source(self, response=None):
        from panmigrate.adapters.aliyun import AliyunSource

        src = AliyunSource({
            "auth_mode": "qr", "access_token": "tk", "expires_at": 0, "drive_id": "",
        })
        # 模拟「登录后第一次就下载」：drive_id 还没查过，要去取
        def fake_drive():
            src.drive_id = "d9"
            return "d9"

        src.ensure_drive = fake_drive
        calls: list[tuple] = []

        def fake_post(path, payload, retries=3):
            calls.append((path, payload))
            return response if response is not None else {"url": "https://x/dl"}

        src._post = fake_post
        return src, calls

    def test_payload_contains_drive_id(self):
        src, calls = self._source()
        url = src._download_url("f1")
        self.assertEqual(url, "https://x/dl")
        path, payload = calls[0]
        self.assertIn("drive_id", payload, "漏了 drive_id 就是 400 both nil")
        self.assertEqual(payload["drive_id"], "d9")
        self.assertEqual(payload["file_id"], "f1")

    def test_drive_id_resolved_when_not_yet_loaded(self):
        """第一次下载时 drive_id 还是空的，必须先解析出来再请求。"""
        src, calls = self._source()
        self.assertEqual(src.drive_id, "")
        src._download_url("f1")
        self.assertEqual(calls[0][1]["drive_id"], "d9")

    def test_cached_url_skips_second_request(self):
        src, calls = self._source()
        src._download_url("f1")
        src._download_url("f1")
        self.assertEqual(len(calls), 1, "同一文件应复用缓存的直链")

    def test_complete_also_sends_drive_id(self):
        """合并分片同理，别指望服务端从 token 推断。"""
        from panmigrate.adapters.base import UploadSession

        sink, calls = make_sink({"file_id": "f9"})
        session = UploadSession(file_id="f1", upload_id="u1", parts=[], part_size=1, size=1)
        sink.complete(session)
        path, payload = calls[0]
        self.assertEqual(payload.get("drive_id"), "d1")

    def test_missing_url_raises_with_server_message(self):
        src, _ = self._source({"code": "NotFound.File", "message": "not found"})
        with self.assertRaises(AliyunError) as ctx:
            src._download_url("f1")
        self.assertIn("下载链接", str(ctx.exception))


if __name__ == "__main__":
    unittest.main()
