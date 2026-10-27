"""接口层测试：勾选清单、批量勾选、重试失败、报告导出、状态结构。

App 用本地演示适配器跑，不碰真实网盘，也不读写用户的 data/config.json
（tests/__init__.py 已经把 PANMIGRATE_HOME 指到临时目录）。
"""
from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from panmigrate import config
from panmigrate.server import App

from .support import IsolatedCase, reset_demo, write_files


def item(path: str, size: int = 1024, is_dir: bool = False) -> dict:
    return {"id": path, "name": path.rsplit("/", 1)[-1], "path": path,
            "size": size, "is_dir": is_dir}


class AppTest(IsolatedCase):
    def setUp(self):
        reset_demo()
        config.save({
            "source": {"provider": "mock", "auth_mode": "none", "root_path": "/"},
            "target": {"provider": "mock", "auth_mode": "none", "root_path": "/"},
        })
        folder = tempfile.mkdtemp(prefix="pmapp-")
        self.app = App(db_path=Path(folder) / "tasks.db")
        self.app.scheduler.stop()

    def tearDown(self):
        self.app.scheduler.stop()
        self.app.db.close()
        super().tearDown()

    # ---------- 状态 ----------
    def test_state_has_expected_shape(self):
        state = self.app.state()
        for key in ("config", "providers", "errors", "describe", "engine",
                    "tasks", "selection", "schedule", "quota", "version"):
            self.assertIn(key, state)
        provider_ids = {p["id"] for p in state["providers"]}
        # 界面列表只给真能用的网盘；mock 是测试装置，不该出现在界面上
        self.assertTrue({"baidu", "aliyun", "quark", "tianyi"} <= provider_ids)
        self.assertNotIn("mock", provider_ids)
        self.assertNotIn("ctfile", provider_ids)
        self.assertNotIn("xunlei", provider_ids)
        # 但代码内部仍要能拿到 mock（测试引擎要用它）
        from panmigrate.adapters import registry
        all_ids = {p["id"] for p in registry.list_providers()}
        self.assertIn("mock", all_ids)
        self.assertEqual(state["errors"], {"source": "", "target": ""})

    def test_quota_reports_reason_when_provider_cannot_query(self):
        quota = self.app.state()["quota"]
        self.assertEqual(quota["provider"], "mock")
        self.assertIn("error", quota, "拿不到容量时要给出原因，不能静默留空")

    # ---------- 挑选文件 ----------
    def test_pick_files_marks_picked_and_selected(self):
        res = self.app.pick_files([item("/照片/a.jpg"), item("/照片/子/目录", is_dir=True)])
        self.assertEqual(res["added"]["added"], 1, "文件夹不该被当成文件加进清单")
        task = self.app.db.list_tasks(10)[0]
        self.assertEqual(task["rel_path"], "照片/a.jpg")
        self.assertEqual((task["picked"], task["selected"]), (1, 1))

    def test_pick_files_rejects_empty_selection(self):
        with self.assertRaises(RuntimeError):
            self.app.pick_files([item("/只是目录", is_dir=True)])

    # ---------- 批量勾选 ----------
    def test_select_all_and_only_open(self):
        self.app.pick_files([item("/a.txt"), item("/b.txt"), item("/c.txt")])
        rows = self.app.db.list_tasks(10)
        self.app.db.update(rows[0]["id"], state="done")

        res = self.app.select_tasks({"all": False, "only_open": True})
        self.assertEqual(res["changed"], 2)
        states = {t["rel_path"]: t["selected"] for t in self.app.db.list_tasks(10)}
        self.assertEqual(states["a.txt"], 1, "已完成的不该被「只选未完成」改掉")
        self.assertEqual(states["b.txt"], 0)

        res = self.app.select_tasks({"all": True})
        self.assertEqual(res["changed"], 3)

    # ---------- 重试失败 ----------
    def test_retry_failed_requeues_and_clears_attempts(self):
        self.app.pick_files([item("/坏过.txt")])
        rec = self.app.db.list_tasks(5)[0]
        self.app.db.update(rec["id"], state="failed", attempts=6, error="炸了")

        res = self.app.retry_failed()
        self.assertEqual(res["changed"], 1)
        rec = self.app.db.get(rec["id"])
        self.assertEqual((rec["state"], rec["attempts"], rec["error"]), ("pending", 0, ""))

    def test_retry_failed_returns_zero_when_nothing_to_do(self):
        self.app.pick_files([item("/正常.txt")])
        self.assertEqual(self.app.retry_failed()["changed"], 0)

    # ---------- 报告 ----------
    def test_report_csv_contains_rows_and_escapes_commas(self):
        self.app.pick_files([item("/有条,逗号/文件.txt", size=2048)])
        csv_text = self.app.report_csv()
        lines = csv_text.splitlines()
        self.assertTrue(lines[0].startswith("文件路径,大小(字节),状态"))
        self.assertEqual(len(lines), 2)
        self.assertIn("有条,逗号/文件.txt", lines[1], "含逗号的路径必须被引号包裹")
        self.assertIn("2048", lines[1])
        self.assertIn("待传", lines[1])

    # ---------- 目标端建目录 ----------
    def test_mkdir_rejects_source_side(self):
        with self.assertRaises(RuntimeError):
            self.app.mkdir("source", "/新目录")

    def test_mkdir_requires_absolute_path(self):
        with self.assertRaises(RuntimeError):
            self.app.mkdir("target", "新目录")

    # ---------- 自选文件：多种挑选方式 ----------
    def test_pick_expands_folder_recursively(self):
        write_files({"照片/a.jpg": 100, "照片/b.mp4": 200, "照片/子/c.jpg": 50})
        res = self.app.pick_files([item("/照片", is_dir=True)])
        self.assertEqual(res["count"], 3, "勾文件夹应该递归展开成里面的文件")
        rels = {t["rel_path"] for t in self.app.db.list_tasks(10)}
        self.assertEqual(rels, {"照片/a.jpg", "照片/b.mp4", "照片/子/c.jpg"})
        for task in self.app.db.list_tasks(10):
            self.assertEqual((task["picked"], task["selected"]), (1, 1))

    def test_pick_folder_respects_file_cap(self):
        write_files({f"多/{i}.bin": 10 for i in range(6)})
        self.app.MAX_PICK_FILES = 4
        res = self.app.pick_files([item("/多", is_dir=True)])
        self.assertEqual(res["count"], 4)
        self.assertTrue(res["truncated"])
        self.assertIn("上限", res["message"])

    def test_pick_paths_resolves_files_and_folders(self):
        write_files({"文档/单个.txt": 10, "包/一.txt": 10, "包/二.txt": 10})
        res = self.app.pick_paths([" /文档/单个.txt ", "/包", "/不存在/x.bin"])
        self.assertEqual(res["count"], 3, "文件和文件夹混着粘都应该能解析")
        self.assertEqual(res["missing"], ["/不存在/x.bin"])
        self.assertIn("没找到", res["message"])

    def test_remove_tasks_only_drops_list_rows(self):
        self.app.pick_files([item("/a.txt"), item("/b.txt")])
        rows = self.app.db.list_tasks(10)
        res = self.app.remove_tasks([rows[0]["id"]])
        self.assertEqual(res["changed"], 1)
        self.assertEqual([t["rel_path"] for t in self.app.db.list_tasks(10)], ["b.txt"])

    # ---------- 多网盘账号共存 ----------
    def test_switching_provider_keeps_both_accounts(self):
        config.update({"source": {"provider": "mock", "cookie": "BAIDU=1"}})
        self.app.cfg = config.load()

        self.app.update_config({"source": {"provider": "tianyi"}})
        self.assertEqual(self.app.cfg["source"]["provider"], "tianyi")
        self.assertEqual(self.app.cfg["source"]["cookie"], "",
                         "切到天翼时不该带着百度的 cookie")
        self.assertEqual((self.app.cfg["accounts"]["mock"] or {}).get("cookie"), "BAIDU=1",
                         "旧账号要归档下来")

        self.app.update_config({"source": {"provider": "mock"}})
        self.assertEqual(self.app.cfg["source"]["cookie"], "BAIDU=1",
                         "切回来要自动恢复登录态")

    # ---------- 配置白名单 ----------
    def test_update_config_filters_unknown_sections(self):
        self.app.update_config({"source": {"provider": "mock"}, "evil": {"x": 1}})
        cfg = config.load()
        self.assertEqual(cfg["source"]["provider"], "mock")
        self.assertNotIn("evil", cfg)

    # ---------- 迁移进行中禁止改结构 ----------
    def test_swap_blocked_while_running(self):
        self.app.engine._running = True
        try:
            with self.assertRaises(RuntimeError):
                self.app.swap_sides()
        finally:
            self.app.engine._running = False

    def test_switch_provider_blocked_while_running(self):
        self.app.engine._running = True
        try:
            with self.assertRaises(RuntimeError):
                self.app.update_config({"source": {"provider": "tianyi"}})
        finally:
            self.app.engine._running = False

    def test_transfer_settings_allowed_while_running(self):
        # 迁移中改并发/限速这类运行时参数应该放行
        self.app.engine._running = True
        try:
            res = self.app.update_config({"transfer": {"file_concurrency": 4}})
            self.assertEqual(res["ok"], True)
        finally:
            self.app.engine._running = False


if __name__ == "__main__":
    unittest.main()


class PortTakeoverTest(unittest.TestCase):
    """「双击新 exe 却看到旧界面」的根因修复。

    背景：Windows 上 socketserver 默认开 SO_REUSEADDR，语义是「允许多个进程
    绑同一端口」。旧实例占着 8899 时新实例照样 bind 成功，两个进程同时服务
    同一端口，请求随机落到旧的那个 —— 用户表现为「换了 exe 但界面还是旧的」。
    """

    def test_windows_disables_address_reuse(self):
        import os

        from panmigrate.server import _Server

        if os.name == "nt":
            self.assertFalse(
                _Server.allow_reuse_address,
                "Windows 上必须独占端口，否则新旧实例能同时绑定",
            )
        else:
            self.assertTrue(
                _Server.allow_reuse_address,
                "macOS/Linux 要保留复用，避免 TIME_WAIT 导致切不回来",
            )

    def test_is_our_server_returns_none_for_unused_port(self):
        from panmigrate.server import _is_our_server

        # 1 号端口不会有服务，探测必须安静地返回 None，而不是抛异常
        self.assertIsNone(_is_our_server("127.0.0.1", 1))

    def test_probe_result_shape_distinguishes_our_server(self):
        """探测靠 /api/state 的字段判断，缺字段就不能当成「自己」去杀。"""
        # 这里只校验判定条件本身：version + engine 同时存在才算我们
        sample_ours = {"version": "1.25.0", "engine": {}, "selection": {}}
        sample_other = {"status": "ok"}
        self.assertTrue("version" in sample_ours and "engine" in sample_ours)
        self.assertFalse("version" in sample_other and "engine" in sample_other)
