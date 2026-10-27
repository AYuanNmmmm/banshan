"""任务库测试：增量计划、手动勾选保护、勾选口径、重新排队、报告行。"""
from __future__ import annotations

import unittest

from .support import IsolatedCase, temp_db


def plan(rel: str, size: int, skip: bool = False) -> dict:
    return {"rel_path": rel, "name": rel.rsplit("/", 1)[-1], "size": size,
            "part_size": 1024 * 1024, "total_parts": 1, "skip": skip}


class PlanTest(IsolatedCase):
    def setUp(self):
        self.db = temp_db("plan")

    def test_add_keep_and_update(self):
        stats = self.db.upsert_plan([plan("a.txt", 10), plan("b.txt", 20)])
        self.assertEqual(stats["added"], 2)

        stats = self.db.upsert_plan([plan("a.txt", 10), plan("b.txt", 20)])
        self.assertEqual((stats["added"], stats["kept"]), (0, 2))

        stats = self.db.upsert_plan([plan("a.txt", 99), plan("b.txt", 20)])
        self.assertEqual(stats["updated"], 1)
        rec = [t for t in self.db.list_tasks(10) if t["rel_path"] == "a.txt"][0]
        self.assertEqual(rec["size"], 99)
        self.assertEqual(rec["state"], "pending")

    def test_size_change_resets_partial_progress(self):
        self.db.upsert_plan([plan("a.txt", 10)])
        rec = self.db.list_tasks(5)[0]
        self.db.update(rec["id"], upload_id="u1", done_bytes=5, done_parts=[1])
        self.db.upsert_plan([plan("a.txt", 20)])
        rec = self.db.get(rec["id"])
        self.assertEqual((rec["upload_id"], rec["done_bytes"], rec["done_parts"]), ("", 0, []))

    def test_stale_pending_task_is_removed(self):
        self.db.upsert_plan([plan("删我.txt", 10)])
        stats = self.db.upsert_plan([plan("留下.txt", 10)])
        self.assertEqual(stats["removed"], 1)
        self.assertEqual([t["rel_path"] for t in self.db.list_tasks(10)], ["留下.txt"])

    def test_manually_picked_task_survives_scan(self):
        self.db.upsert_plan([plan("手选/单独.txt", 10)])
        self.db.mark_picked(["手选/单独.txt"])
        stats = self.db.upsert_plan([plan("别的/其它.txt", 10)])
        self.assertEqual(stats["removed"], 0)
        rels = {t["rel_path"] for t in self.db.list_tasks(10)}
        self.assertIn("手选/单独.txt", rels)

    def test_done_task_is_requeued_when_target_file_disappeared(self):
        self.db.upsert_plan([plan("曾经传完.txt", 10)])
        rec = self.db.list_tasks(5)[0]
        self.db.update(rec["id"], state="done", done_bytes=10)
        self.db.upsert_plan([plan("曾经传完.txt", 10, skip=False)])
        rec = self.db.get(rec["id"])
        self.assertEqual(rec["state"], "pending")
        self.assertEqual(rec["done_bytes"], 0)

    def test_finished_task_kept_when_target_still_there(self):
        self.db.upsert_plan([plan("还在.txt", 10)])
        rec = self.db.list_tasks(5)[0]
        self.db.update(rec["id"], state="done", done_bytes=10)
        stats = self.db.upsert_plan([plan("还在.txt", 10, skip=True)])
        self.assertEqual(stats["kept"], 1)
        self.assertEqual(self.db.get(rec["id"])["state"], "done")


class SelectionTest(IsolatedCase):
    def setUp(self):
        self.db = temp_db("sel")
        self.db.upsert_plan([plan("a", 100), plan("b", 200), plan("c", 300)])
        self.ids = [t["id"] for t in self.db.list_tasks(10)]

    def test_selection_counters(self):
        self.db.set_selected([self.ids[0]], False)
        sel = self.db.selection()
        self.assertEqual((sel["total"], sel["picked"], sel["picked_bytes"]), (3, 2, 500))

    def test_summary_only_counts_selected(self):
        self.db.set_selected([self.ids[0]], False)
        summary = self.db.summary()
        self.assertEqual(summary["total"], 2)
        self.assertEqual(summary["total_bytes"], 500)

    def test_only_open_excludes_finished(self):
        self.db.update(self.ids[0], state="done")
        changed = self.db.set_selected_all(False, only_open=True)
        self.assertEqual(changed, 2)
        states = {t["rel_path"]: t["selected"] for t in self.db.list_tasks(10)}
        self.assertEqual(states["a"], 1, "已完成的不该被「只选未完成」动到")
        self.assertEqual(states["b"], 0)

    def test_pending_ids_only_includes_selected_and_open(self):
        self.db.update(self.ids[0], state="done")
        self.db.set_selected([self.ids[1]], False)
        self.assertEqual(self.db.pending_ids(), [self.ids[2]])


class RequeueTest(IsolatedCase):
    def setUp(self):
        self.db = temp_db("requeue")
        self.db.upsert_plan([plan("坏过.txt", 10), plan("正常.txt", 10)])

    def test_resets_attempts_and_error(self):
        rec = [t for t in self.db.list_tasks(10) if t["rel_path"] == "坏过.txt"][0]
        self.db.update(rec["id"], state="failed", attempts=7, error="炸了", selected=0)
        changed = self.db.requeue()
        self.assertEqual(changed, 1)
        rec = self.db.get(rec["id"])
        self.assertEqual((rec["state"], rec["attempts"], rec["error"], rec["selected"]),
                         ("pending", 0, "", 1))

    def test_requeue_by_id_only_touches_that_task(self):
        rows = self.db.list_tasks(10)
        self.db.update(rows[0]["id"], state="failed")
        self.db.update(rows[1]["id"], state="failed")
        changed = self.db.requeue([rows[0]["id"]])
        self.assertEqual(changed, 1)
        self.assertEqual(self.db.get(rows[0]["id"])["state"], "pending")
        self.assertEqual(self.db.get(rows[1]["id"])["state"], "failed")


class ReportTest(IsolatedCase):
    def test_report_rows_include_part_progress(self):
        db = temp_db("report")
        db.upsert_plan([plan("报告.txt", 10)])
        rec = db.list_tasks(5)[0]
        db.update(rec["id"], done_parts=[1, 2, 3])
        row = db.report_rows()[0]
        self.assertEqual(row["rel_path"], "报告.txt")
        self.assertEqual(row["done_parts_count"], 3)
        self.assertEqual(row["state"], "pending")


class SkipReasonTest(IsolatedCase):
    """「注定传不了」的文件要在扫描阶段就落库并写明原因。"""

    def setUp(self):
        self.db = temp_db("skipreason")

    def test_skip_reason_is_stored(self):
        self.db.upsert_plan([
            {"rel_path": "空.txt", "size": 0, "skip": True,
             "skip_reason": "百度网盘不支持 0 字节文件，已跳过"},
        ])
        rec = self.db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "skipped")
        self.assertIn("0 字节", rec["skip_reason"])

    def test_permanent_skip_is_not_requeued(self):
        """带 skip_reason 的跳过是永久的，目标端没这文件也不该重排。"""
        self.db.upsert_plan([
            {"rel_path": "空.txt", "size": 0, "skip": True, "skip_reason": "不支持空文件"},
        ])
        # 再扫一次，这次目标端仍然没有它
        self.db.upsert_plan([{"rel_path": "空.txt", "size": 0, "skip": False}])
        rec = self.db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "skipped")
        self.assertEqual(rec["skip_reason"], "不支持空文件")

    def test_repeated_scan_does_not_rewrite(self):
        """每轮扫描都写库会白白制造 updated，同一个原因应算 kept。"""
        item = {"rel_path": "空.txt", "size": 0, "skip": True, "skip_reason": "不支持空文件"}
        self.db.upsert_plan([item])
        stats = self.db.upsert_plan([item])
        self.assertEqual(stats["updated"], 0)
        self.assertEqual(stats["kept"], 1)


if __name__ == "__main__":
    unittest.main()
