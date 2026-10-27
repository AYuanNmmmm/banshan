"""传输引擎测试：分片计算、端到端、增量、暂停续传、校验、重试、并发、限速。

这些用例都不联网：两端都用本地演示适配器（data/demo 下的两个文件夹）。
"""
from __future__ import annotations

import time
import unittest

from panmigrate.engine import MIN_PART, calc_parts
from panmigrate.adapters.mock import DEMO_ROOT

from .support import (
    IsolatedCase,
    CorruptSink,
    FlakySink,
    ProgressSource,
    RaceProbeSink,
    RapidSink,
    make_engine,
    reset_demo,
    run_to_end,
    sha1_bytes,
    sha1_file,
    target_path,
    write_files,
)

MB = 1024 * 1024


class CalcPartsTest(unittest.TestCase):
    def test_zero_and_empty_file(self):
        part_size, total = calc_parts(0, 8)
        self.assertEqual((part_size, total), (8 * MB, 1))

    def test_exact_multiple_does_not_add_part(self):
        self.assertEqual(calc_parts(8 * MB, 8), (8 * MB, 1))
        self.assertEqual(calc_parts(16 * MB, 8), (8 * MB, 2))

    def test_one_byte_over_adds_a_part(self):
        part_size, total = calc_parts(8 * MB + 1, 8)
        self.assertEqual(total, 2)
        self.assertEqual(part_size, 8 * MB)

    def test_huge_file_grows_part_size_under_part_cap(self):
        size = 16 * 1024 * MB  # 16 GB
        part_size, total = calc_parts(size, 8)
        self.assertLessEqual(total, 1000, "分片数必须被限制在 1000 以内")
        self.assertEqual(total, (size + part_size - 1) // part_size)
        self.assertGreaterEqual(part_size, 8 * MB)

    def test_part_size_is_aligned(self):
        for size in (1, 12345, 8 * MB, 300 * MB, 3 * 1024 * MB):
            part_size, _ = calc_parts(size, 3)
            self.assertEqual(part_size % MIN_PART, 0, f"{size} 的分片大小没对齐 256KB")


class EngineFlowTest(IsolatedCase):
    def setUp(self):
        reset_demo()

    def test_end_to_end_then_incremental(self):
        made = write_files({
            "文档/说明.txt": 12,
            "文档/空文件.txt": 0,
            "视频/大文件.bin": 2 * MB + 7,
            "备份/深层/再深一层/数据.md": 512,
        })
        engine, db = make_engine({"part_size_mb": 1, "part_concurrency": 3})

        scan = engine.scan()
        self.assertEqual(scan["files"], 4)
        self.assertEqual(scan["added"], 4)

        engine.start()
        run_to_end(engine)

        summary = db.summary()
        self.assertEqual(summary["done"], 4, f"有文件没传完：{db.list_tasks()}")
        self.assertEqual(summary["failed"], 0)
        for rel, data in made.items():
            self.assertTrue(target_path(rel).exists(), f"{rel} 没落到目标端")
            self.assertEqual(sha1_file(target_path(rel)), sha1_bytes(data), f"{rel} 内容不一致")

        again = engine.scan()
        self.assertEqual(again["skip"], 4, "第二次扫描应该全部识别为已存在")
        self.assertEqual(again["added"], 0)

    def test_multi_part_file_assembles_in_order(self):
        made = write_files({"多分片.bin": 3 * MB + 99})
        engine, db = make_engine({"part_size_mb": 1})
        engine.scan()
        engine.start()
        run_to_end(engine)
        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "done")
        self.assertEqual(rec["total_parts"], 4)
        self.assertEqual(sha1_file(target_path("多分片.bin")), sha1_bytes(made["多分片.bin"]))

    def test_deleted_target_file_is_requeued_on_rescan(self):
        """目标端文件被人删了，再扫一次必须重新排队 —— 否则「增量」等于漏传。"""
        write_files({"会被删.txt": 256 * 1024})
        engine, db = make_engine({"part_size_mb": 1})
        engine.scan()
        engine.start()
        run_to_end(engine)
        self.assertEqual(db.list_tasks(5)[0]["state"], "done")

        target_path("会被删.txt").unlink()
        engine.scan()
        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "pending", "目标端缺失的文件没有被重新排队")
        self.assertEqual(rec["done_bytes"], 0)

        engine.start()
        run_to_end(engine)
        self.assertEqual(db.list_tasks(5)[0]["state"], "done")
        self.assertTrue(target_path("会被删.txt").exists())

    def test_pause_stops_at_part_boundary_and_resumes(self):
        made = write_files({"大文件.bin": 2 * MB})
        engine, db = make_engine({
            "part_size_mb": 1, "file_concurrency": 1, "part_concurrency": 1,
            "bandwidth_kbps": 512,  # 限速，保证有足够时间在中途喊停
        })
        engine.scan()
        engine.start()
        time.sleep(0.6)
        engine.pause()
        run_to_end(engine, timeout=15)

        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "paused")
        self.assertGreater(rec["done_bytes"], 0, "暂停前应该已经传了一部分")
        self.assertLess(rec["done_bytes"], rec["size"], "暂停没有真的打断传输")

        engine.settings["bandwidth_kbps"] = 0  # 恢复时不再限速，省测试时间
        engine.start()
        run_to_end(engine)
        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "done")
        self.assertEqual(sha1_file(target_path("大文件.bin")), sha1_bytes(made["大文件.bin"]))

    def test_verification_catches_silent_corruption(self):
        write_files({"被改坏.bin": 512 * 1024})
        engine, db = make_engine({"part_size_mb": 1, "retries": 0}, sink=CorruptSink())
        engine.scan()
        engine.start()
        run_to_end(engine)
        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "failed", "内容被改写却判成功，校验没起作用")
        self.assertIn("校验", rec["error"])

    def test_transient_error_is_retried(self):
        made = write_files({"偶发失败.bin": 512 * 1024})
        engine, db = make_engine({"part_size_mb": 1, "retries": 1}, sink=FlakySink())
        engine.scan()
        engine.start()
        run_to_end(engine)
        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "done", f"一次瞬时失败就该重试成功：{rec['error']}")
        self.assertEqual(sha1_file(target_path("偶发失败.bin")), sha1_bytes(made["偶发失败.bin"]))

    def test_exhausted_attempts_can_be_requeued(self):
        """回归：attempts 累计超过 retries 后，旧代码会静默卡在 failed 永不重跑。"""
        write_files({"重试样本.txt": 1024})
        engine, db = make_engine({"part_size_mb": 1, "retries": 0})
        engine.scan()
        task_id = db.list_tasks(5)[0]["id"]
        db.update(task_id, state="failed", attempts=9, error="人为制造的历史失败")

        db.requeue()
        engine.start()
        run_to_end(engine)
        rec = db.get(task_id)
        self.assertEqual(rec["state"], "done", "重新排队后仍然没有重跑")
        self.assertTrue(target_path("重试样本.txt").exists())

    def test_rapid_session_marks_done_without_uploading(self):
        """服务端已存在该文件（不给 upload_id）时，应该 0 分片收下，而不是判失败。"""
        made = write_files({"已存在.bin": 300 * 1024})
        sink = RapidSink(made["已存在.bin"])
        engine, db = make_engine({"part_size_mb": 1, "retries": 0}, sink=sink)
        engine.scan()
        engine.start()
        run_to_end(engine)

        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "done", f"秒传命中却判失败：{rec['error']}")
        self.assertEqual(sink.put_calls, 0, "秒传命中时不该再传任何分片")
        self.assertEqual(sha1_file(target_path("已存在.bin")), sha1_bytes(made["已存在.bin"]))

    def test_flat_structure_puts_files_in_target_root(self):
        write_files({"深/层/子/file.txt": 256})
        engine, db = make_engine({"part_size_mb": 1, "structure": "flat"})
        engine.scan()
        engine.start()
        run_to_end(engine)
        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "done")
        self.assertTrue(target_path("file.txt").exists(), "平铺后文件应直接落在目标目录")
        self.assertFalse((DEMO_ROOT / "target" / "深").exists(), "平铺模式不该再建源目录层级")

    def test_conflict_auto_rename_keeps_both(self):
        made = write_files({"同名.txt": 1024})
        old = target_path("同名.txt")
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"x" * 10)  # 目标端已有同名但内容不同的文件

        engine, db = make_engine({"part_size_mb": 1, "conflict": "auto_rename"})
        engine.scan()
        engine.start()
        run_to_end(engine)

        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "done", f"自动改名不该失败：{rec['error']}")
        self.assertEqual(old.read_bytes(), b"x" * 10, "原文件不能被覆盖")
        renamed = target_path("同名 (1).txt")
        self.assertTrue(renamed.exists(), "重名文件应该以新名字落地")
        self.assertEqual(sha1_file(renamed), sha1_bytes(made["同名.txt"]))

    def test_conflict_skip_keeps_original_and_marks_reason(self):
        write_files({"冲突.txt": 2048})
        old = target_path("冲突.txt")
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"y" * 10)

        engine, db = make_engine({"part_size_mb": 1, "conflict": "skip"})
        engine.scan()
        engine.start()
        run_to_end(engine)

        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "skipped")
        self.assertIn("同名", rec["error"])
        self.assertEqual(old.read_bytes(), b"y" * 10)
        self.assertEqual(db.summary()["failed"], 0)

    def test_conflict_refuse_fails_loudly(self):
        write_files({"拒绝.txt": 4096})
        old = target_path("拒绝.txt")
        old.parent.mkdir(parents=True, exist_ok=True)
        old.write_bytes(b"z" * 10)

        engine, db = make_engine({"part_size_mb": 1, "conflict": "refuse", "retries": 0})
        engine.scan()
        engine.start()
        run_to_end(engine)

        rec = db.list_tasks(5)[0]
        self.assertEqual(rec["state"], "failed")
        self.assertIn("同名", rec["error"])

    def test_concurrent_transfers_do_not_create_dir_twice(self):
        write_files({f"同一层/文件{i}.bin": 128 * 1024 for i in range(6)})
        sink = RaceProbeSink()
        engine, db = make_engine({"part_size_mb": 1, "file_concurrency": 4}, sink=sink)
        engine.scan()
        engine.start()
        run_to_end(engine)

        self.assertEqual(db.summary()["failed"], 0)
        self.assertEqual(db.summary()["done"], 6)
        self.assertFalse(sink.overlap, "同名目录被并发创建了，目录锁没生效")

    def test_bandwidth_limit_slows_transfer(self):
        write_files({"限速.bin": 512 * 1024})

        fast_engine, _ = make_engine({"part_size_mb": 1, "bandwidth_kbps": 0})
        fast_engine.scan()
        t0 = time.time()
        fast_engine.start()
        run_to_end(fast_engine)
        fast = time.time() - t0

        reset_demo()
        write_files({"限速.bin": 512 * 1024})
        slow_engine, _ = make_engine({"part_size_mb": 1, "bandwidth_kbps": 256})
        slow_engine.scan()
        t0 = time.time()
        slow_engine.start()
        run_to_end(slow_engine)
        slow = time.time() - t0

        self.assertLess(fast, 1.5, "不限速时不该这么慢")
        self.assertGreater(slow, 1.5, f"限速 256KB/s 传 512KB 至少 2 秒，实际 {slow:.2f}s")


class PrecheckTest(IsolatedCase):
    """扫描阶段就要把「注定传不了」的文件标出来，别等传一半才失败。"""

    def setUp(self):
        reset_demo()

    def test_empty_file_skipped_when_sink_rejects(self):
        write_files({"空.txt": 0, "正常.txt": 400})
        engine, db = make_engine({})
        engine.sink.supports_empty_file = False     # 模拟百度/夸克/天翼
        res = engine.scan()
        self.assertEqual(res["unsupported"], 1, "应报告 1 个不支持的文件")

        rows = {t["rel_path"]: t for t in db.list_tasks(10)}
        self.assertEqual(rows["空.txt"]["state"], "skipped")
        self.assertIn("0 字节", rows["空.txt"]["skip_reason"])
        self.assertEqual(rows["正常.txt"]["state"], "pending")

    def test_normal_sink_keeps_empty_file(self):
        write_files({"空.txt": 0})
        engine, db = make_engine({})
        res = engine.scan()
        self.assertEqual(res["unsupported"], 0)
        self.assertEqual(db.list_tasks(5)[0]["state"], "pending")

    def test_long_name_skipped(self):
        write_files({"正常.txt": 100})
        engine, db = make_engine({})
        engine.sink.max_name_bytes = 4               # "正常.txt" 远超 4 字节
        res = engine.scan()
        self.assertEqual(res["unsupported"], 1)
        self.assertIn("文件名超过", db.list_tasks(5)[0]["skip_reason"])


class FatalAuthTest(IsolatedCase):
    """登录态类错误不该重试，而且应中止整轮。"""

    def setUp(self):
        reset_demo()

    def test_auth_error_stops_whole_run(self):
        from panmigrate.adapters.base import AuthError

        write_files({"a.txt": 100, "b.txt": 100, "c.txt": 100})
        engine, db = make_engine({"file_concurrency": 1, "retries": 3})
        engine.scan()

        def boom(task):
            raise AuthError("未授权：请先扫码登录")

        engine._transfer = boom
        engine.start()
        run_to_end(engine)

        s = db.summary()
        # 关键：只有第一个被判失败，后面的文件没有被逐个撞墙
        self.assertEqual(s["failed"], 1, f"应只失败 1 个，实际 {s}")
        self.assertTrue(engine._fatal, "应记录中止原因")
        self.assertIn("未授权", engine._fatal)

    def test_retryable_error_still_retries(self):
        from panmigrate.adapters.base import AdapterError

        write_files({"a.txt": 100})
        engine, db = make_engine({"retries": 1, "file_concurrency": 1})
        engine.scan()
        calls = {"n": 0}

        def flaky(task):
            calls["n"] += 1
            raise AdapterError("临时网络错误")

        engine._transfer = flaky
        engine.start()
        run_to_end(engine)
        self.assertEqual(calls["n"], 2, "可重试的错误仍应重试 1 次（共 2 次）")
        self.assertFalse(engine._fatal)


class ActiveSnapshotTest(IsolatedCase):
    """snapshot 要能说清「正在传哪个文件、第几片」。"""

    def setUp(self):
        reset_demo()

    def test_active_lists_running_task_with_part_progress(self):
        write_files({"大.bin": 3 * 1024 * 1024})
        engine, db = make_engine({"part_size_mb": 1, "part_concurrency": 1,
                                  "file_concurrency": 1})
        engine.scan()
        rec = db.list_tasks(5)[0]
        # 手动把它标成 running 并给一点分片进度，模拟传输中
        db.update(rec["id"], state="running", total_parts=3, done_parts=[1, 2],
                  done_bytes=1024 * 1024)
        snap = engine.snapshot()
        self.assertEqual(len(snap["active"]), 1)
        a = snap["active"][0]
        self.assertEqual(a["rel"], "大.bin")
        self.assertEqual(a["done_parts"], 2)
        self.assertEqual(a["total_parts"], 3)


class SourceConcurrencyCapTest(IsolatedCase):
    """源端有并发上限时，引擎要自动收敛，别让线程白等。

    百度非会员总带宽约 100KB/s，用户把并发开到 6 只会让每条连接都慢到
    被服务端掐断（IncompleteRead）。BaiduSource.MAX_CONCURRENT = 2。
    """

    def setUp(self):
        reset_demo()

    def test_workers_capped_by_source_limit(self):
        write_files({"a.bin": 1024})
        engine, _ = make_engine({"file_concurrency": 8, "part_concurrency": 8})
        engine.source.MAX_CONCURRENT = 2
        engine.scan()
        engine.start()
        run_to_end(engine)
        self.assertEqual(len(engine._workers), 2, "文件并发应被源端上限收敛到 2")

    def test_no_limit_keeps_configured_workers(self):
        write_files({"a.bin": 1024})
        engine, _ = make_engine({"file_concurrency": 3})
        # 默认 MockSource 没有 MAX_CONCURRENT，按配置走
        engine.scan()
        engine.start()
        run_to_end(engine)
        self.assertEqual(len(engine._workers), 3)


class SequentialSinkTest(IsolatedCase):
    """目标端要求顺序上传时（阿里云盘），必须一片一片按 1,2,3… 传。

    阿里云盘是 OSS sequential multipart：跳号或并发都返回 400
    `PartNotSequential — For sequential multipart upload, you must upload or
    complete parts with sequential part number.`
    """

    def setUp(self):
        reset_demo()

    def test_aliyun_declares_sequential(self):
        from panmigrate.adapters.aliyun import AliyunSink

        self.assertTrue(AliyunSink.sequential_parts)
        # 别的网盘是普通 multipart，允许并发，不能被误伤
        from panmigrate.adapters.baidu import BaiduSink
        from panmigrate.adapters.quark import QuarkSink
        from panmigrate.adapters.tianyi import TianyiSink

        for cls in (BaiduSink, QuarkSink, TianyiSink):
            self.assertFalse(cls.sequential_parts, f"{cls.__name__} 不该被判成顺序上传")

    def test_parts_uploaded_in_order_one_at_a_time(self):
        """即使并发设成 4，顺序型目标端也只会一片一片按序传。"""
        import threading

        write_files({"大.bin": 4 * 1024 * 1024})
        engine, db = make_engine(
            {"part_size_mb": 1, "part_concurrency": 4, "file_concurrency": 1}
        )
        engine.sink.sequential_parts = True

        order: list[int] = []
        live = {"now": 0, "peak": 0}
        lock = threading.Lock()
        orig = engine.sink.put_part

        def wrapped(url, data, number):
            with lock:
                live["now"] += 1
                live["peak"] = max(live["peak"], live["now"])
            order.append(number)
            try:
                return orig(url, data, number)
            finally:
                with lock:
                    live["now"] -= 1

        engine.sink.put_part = wrapped
        engine.scan()
        engine.start()
        run_to_end(engine)

        self.assertEqual(order, [1, 2, 3, 4], "必须严格按分片号递增上传")
        self.assertEqual(live["peak"], 1, "顺序上传时同时只能有 1 个分片在传")
        self.assertEqual(db.summary()["done"], 1)

    def test_holes_in_done_parts_are_treated_as_stale(self):
        """已完成分片有空洞（历史并发会话）时，会话对顺序上传已不可用，应重建。"""
        from panmigrate.adapters.base import AdapterError, RemoteItem, UploadSession
        from panmigrate.engine import _looks_stale

        engine, db = make_engine({})
        engine.sink.sequential_parts = True
        parts = [{"part_number": i, "upload_url": "https://x/"} for i in (1, 2, 3, 4)]
        session = UploadSession(
            file_id="f", upload_id="u", parts=parts, part_size=1024, size=4096
        )
        # 只传 2 和 4 → 换句话说已完成的是 {1,3}，中间缺 2，不符合 1..k 连续前缀
        pending = [parts[1], parts[3]]
        item = RemoteItem(id="1", name="a", is_dir=False, path="/a", size=4096)

        with self.assertRaises(AdapterError) as ctx:
            engine._upload_parts({"id": 1, "rel_path": "a"}, item, session, pending, 1024, 4096)
        msg = str(ctx.exception)
        self.assertIn("不连续", msg)
        self.assertTrue(
            _looks_stale(ctx.exception), "要能被上层认成会话失效，从而重建后重传"
        )


class ProgressAccountingTest(IsolatedCase):
    """进度必须等于「已落库字节 + 未落库字节」，不能重复计算。

    曾经出现过「已传 395MB / 总 263MB（100%）」—— 原因是上传失败重试时
    已读字节没退掉，加上任务完成后整文件字节又被算了一次。
    """

    def setUp(self):
        reset_demo()

    def test_done_equals_total_after_finish(self):
        write_files({"a.bin": 3 * 1024 * 1024})
        engine, db = make_engine(
            {"part_size_mb": 1, "part_concurrency": 2}, source=ProgressSource({})
        )
        engine.scan()
        engine.start()
        run_to_end(engine)

        snap = engine.snapshot()
        total = snap["summary"]["total_bytes"]
        self.assertEqual(snap["done_bytes"], total, "传完后已传字节应正好等于总量")
        self.assertEqual(snap["remaining"], 0)
        # 关键不变量：全部落库后，「已读但未落库」的计数必须归零。
        # 只断言 done_bytes 不够 —— snapshot 里有 min(total, ...) 兜底，
        # 会把泄漏的字节截掉，掩盖问题。
        self.assertEqual(engine._bytes_live, 0, f"仍有 {engine._bytes_live} 字节未落库也没退掉")

    def test_upload_retry_does_not_leak_bytes(self):
        """分片上传失败重试时，同一批字节不能被重复计入进度。"""
        write_files({"a.bin": 2 * 1024 * 1024})
        sink = FlakySink()   # 第 1 片上传来一次 IOError
        engine, db = make_engine(
            {"part_size_mb": 1, "part_concurrency": 1, "retries": 2},
            sink=sink,
            source=ProgressSource({}),
        )
        engine.scan()
        engine.start()
        run_to_end(engine)

        snap = engine.snapshot()
        total = snap["summary"]["total_bytes"]
        self.assertEqual(db.summary()["done"], 1, "失败一次后应重试成功")
        self.assertLessEqual(
            snap["done_bytes"], total, f"进度 {snap['done_bytes']} 超过了总量 {total}"
        )
        self.assertEqual(snap["remaining"], 0)
        # 失败重试读到的字节必须退掉，否则会一直堆在 _bytes_live 里
        self.assertEqual(
            engine._bytes_live, 0, f"重试泄漏了 {engine._bytes_live} 字节"
        )


class ResetRunningTest(IsolatedCase):
    """上次被强杀/直接关窗口时，任务会留在 running。

    不复位的话：pending_ids() 选不到它（点开始迁移提示「没有待迁移的任务」），
    界面却一直显示「正在传 第 N/M 片」，看着像卡死。
    """

    def setUp(self):
        reset_demo()

    def test_stuck_running_is_requeued_on_startup(self):
        from panmigrate.engine import TransferEngine

        write_files({"a.bin": 100})
        engine, db = make_engine({})
        engine.scan()
        task = db.list_tasks(1)[0]
        # 模拟「传到一半被强杀」：state 停在 running，但有分片进度
        db.update(task["id"], state="running", done_parts=[1], done_bytes=60)
        # 卡住的 running 会让界面显示「正在处理 1 个」，但其实根本没在跑
        self.assertEqual(db.summary()["running"], 1)

        # 模拟重新启动程序
        engine2 = TransferEngine(db, {})
        again = db.list_tasks(1)[0]
        self.assertEqual(again["state"], "pending", "启动时应把卡住的 running 退回待处理")
        self.assertEqual(again["done_parts"], [1], "已传分片要保留，别让用户重传")
        self.assertEqual(again["done_bytes"], 60)
        self.assertEqual(db.summary()["running"], 0, "复位后不该还显示「正在处理」")
        self.assertEqual(len(db.pending_ids()), 1, "复位后仍要能被「开始迁移」选中")
        self.assertTrue(any("退回待处理" in l["msg"] for l in engine2._logs))

    def test_completed_tasks_untouched(self):
        """只动 running，已完成/跳过/失败的不能被一起改掉。"""
        from panmigrate.engine import TransferEngine

        write_files({"a.bin": 100, "b.bin": 100})
        engine, db = make_engine({})
        engine.scan()
        rows = db.list_tasks(5)
        db.update(rows[0]["id"], state="done")
        db.update(rows[1]["id"], state="running")

        TransferEngine(db, {})
        states = {t["rel_path"]: t["state"] for t in db.list_tasks(5)}
        self.assertEqual(states["a.bin"], "done")
        self.assertEqual(states["b.bin"], "pending")


class PauseInterruptTest(IsolatedCase):
    """暂停要能中断正在读的分片，否则按钮会灰几十秒，点开始还被顶回来。"""

    def test_fetch_segment_honours_stop(self):
        from panmigrate.adapters.base import ReadCancelled, RemoteItem, SourceAdapter
        import threading

        class Src(SourceAdapter):
            read_max_bytes = 64 * 1024

            def describe(self):
                return {}

            def list_dir(self, path):
                return []

            def _prepare_download(self, item, force_refresh=False):
                return "https://example/x", {}

        src = Src()
        ev = threading.Event()
        src.stop_event = ev
        ev.set()   # 已经点了暂停

        item = RemoteItem(id="1", name="a", is_dir=False, path="/a", size=1024)
        with self.assertRaises(ReadCancelled):
            src._fetch_segment(item, 0, 1024)

    def test_resumable_read_honours_stop(self):
        from panmigrate.adapters.base import ReadCancelled, RemoteItem, SourceAdapter
        import threading

        class Src(SourceAdapter):
            read_max_bytes = 64 * 1024

            def describe(self):
                return {}

            def list_dir(self, path):
                return []

            def _prepare_download(self, item, force_refresh=False):
                return "https://example/x", {}

        src = Src()
        ev = threading.Event()
        src.stop_event = ev
        ev.set()

        item = RemoteItem(id="1", name="a", is_dir=False, path="/a", size=1024)
        with self.assertRaises(ReadCancelled):
            src.read_range(item, 0, 1024)

    def test_attach_wires_stop_event_into_source(self):
        """引擎必须把停止信号交给源端，否则中断机制形同虚设。"""
        engine, db = make_engine({})
        self.assertIs(engine.source.stop_event, engine._stop)


class StaleRunningTakeoverTest(IsolatedCase):
    """_running 卡在 True 但 worker 都退了时，start() 要能接管。"""

    def setUp(self):
        reset_demo()

    def test_start_takes_over_when_no_worker_alive(self):
        write_files({"a.bin": 100})
        engine, db = make_engine({"file_concurrency": 1})
        engine.scan()
        # 模拟：标志没复位，但线程都不在了
        engine._running = True
        engine._workers = []

        res = engine.start()
        self.assertTrue(res.get("ok"), f"应该能接管启动，实际 {res}")
        run_to_end(engine)
        self.assertEqual(db.summary()["done"], 1)
        self.assertTrue(any("自动接管" in l["msg"] for l in engine._logs))

    def test_start_refuses_while_worker_still_running(self):
        """真的还有 worker 在跑时仍要拒绝，不能两个一起上。"""
        write_files({"a.bin": 100})
        engine, db = make_engine({})
        engine.scan()
        import threading

        stop = threading.Event()
        t = threading.Thread(target=stop.wait, daemon=True)
        t.start()
        engine._running = True
        engine._workers = [t]
        try:
            res = engine.start()
            self.assertFalse(res.get("ok"))
            self.assertIn("收尾", res.get("msg") or "")
        finally:
            stop.set()
            t.join(timeout=2)


class PauseLatencyTest(IsolatedCase):
    """暂停必须在 1 秒内真正停下。

    原来要等当前整个分片传完才肯退出（10MB @ 限速 ≈ 几十秒），
    期间「开始迁移」是灰的，点了还被「已经在跑了」顶回来。
    """

    def setUp(self):
        reset_demo()

    def test_pause_stops_within_a_second(self):
        from unittest import mock

        from panmigrate.adapters import base as basemod
        from panmigrate.adapters.base import SourceAdapter

        class SlowResp:
            """每次 read(64KB) 睡 20ms，模拟限速网盘。"""

            def __init__(self, total):
                self.left = total

            def __enter__(self):
                return self

            def __exit__(self, *exc):
                return False

            def read(self, n=-1):
                if self.left <= 0:
                    return b""
                time.sleep(0.02)
                k = min(n if n and n > 0 else self.left, self.left)
                self.left -= k
                return b"x" * k

        class SlowSrc(SourceAdapter):
            read_max_bytes = 256 * 1024
            min_segment = 64 * 1024
            MAX_CONCURRENT = 1

            def describe(self):
                return {}

            def list_dir(self, path):
                return []

            def _prepare_download(self, item, force_refresh=False):
                return "https://example/x", {}

        def fake_urlopen(req, timeout=None):
            a, b = req.headers["Range"].replace("bytes=", "").split("-")
            return SlowResp(int(b) - int(a) + 1)

        MB = 1024 * 1024
        engine, db = make_engine(
            {"file_concurrency": 1, "part_concurrency": 1, "part_size_mb": 1, "retries": 3},
            source=SlowSrc(),
        )
        db.upsert_plan([{
            "rel_path": "慢.bin", "name": "慢.bin", "src_id": "x",
            "size": 8 * MB, "part_size": MB, "total_parts": 8,
        }])

        with mock.patch.object(basemod.urllib.request, "urlopen", side_effect=fake_urlopen):
            self.assertTrue(engine.start().get("ok"))
            time.sleep(0.5)
            self.assertTrue(engine.running, "0.5 秒后应该还在传")

            t0 = time.time()
            engine.pause()
            while engine.running and time.time() - t0 < 10:
                time.sleep(0.02)
            elapsed = time.time() - t0

        self.assertFalse(engine.running)
        self.assertLess(elapsed, 1.5, f"暂停用了 {elapsed:.2f} 秒才生效，太久了")

        task = db.list_tasks(1)[0]
        self.assertEqual(task["state"], "paused", "暂停不该被记成失败")
        self.assertEqual(task.get("error") or "", "", "暂停不该留下错误信息")
        # 已传完的分片要保留，继续时不必重传
        self.assertTrue(task.get("done_parts"), "已传分片丢了")


if __name__ == "__main__":
    unittest.main()


class StopTransferTest(IsolatedCase):
    """「停止」= 中止迁移 + 取消所有勾选。

    注意和原来那个「清空任务」的区别：那个后端调的是 db.clear()，
    和清单里的「全部移除」完全一样，属于重复入口，已经移除。
    """

    def setUp(self):
        reset_demo()

    def test_stop_clears_all_selection_but_keeps_tasks(self):
        from panmigrate.server import App

        write_files({"a.bin": 100, "b.bin": 200})
        engine, db = make_engine({})
        engine.scan()
        self.assertEqual(db.selection()["picked"], 2, "扫描后默认全选")

        app = App.__new__(App)          # 只借方法，不跑完整初始化
        app.engine = engine
        app.db = db

        res = app.stop_transfer()
        self.assertTrue(res["ok"])
        self.assertFalse(res["stopped"], "没在跑的时候不算「停止了一轮」")
        self.assertEqual(db.selection()["picked"], 0, "勾选应全部取消")
        # 关键：任务记录还在，重新勾选就能继续
        self.assertEqual(db.selection()["total"], 2, "停止不该删任务记录")

    def test_stop_is_not_the_same_as_clear(self):
        """清空任务已移除：它的 db.clear() 会连记录一起删，和「全部移除」重复。"""
        write_files({"a.bin": 100})
        engine, db = make_engine({})
        engine.scan()
        self.assertEqual(db.selection()["total"], 1)
        db.set_selected_all(False, False)
        self.assertEqual(db.selection()["total"], 1, "只取消勾选，记录必须留着")

    def test_stop_from_paused_resets_paused_flag(self):
        """从「暂停」直接点停止后，状态必须回到空闲。

        原来 paused 的定义是 `not _running and _stop.is_set()`，
        而暂停本来就置了 _stop —— 停止时又不再调 pause()，标志就一直留着，
        界面上明明停下来了却还显示「已暂停」（QA 报告问题 5）。
        """
        from panmigrate.server import App

        write_files({"a.bin": 100})
        engine, db = make_engine({})
        engine.scan()

        app = App.__new__(App)
        app.engine = engine
        app.db = db

        engine.pause()
        time.sleep(0.3)
        self.assertTrue(engine.snapshot()["paused"], "先确认确实处于暂停态")

        res = app.stop_transfer()
        self.assertTrue(res["ok"])
        self.assertFalse(engine.snapshot()["paused"], "停止之后不该再是「已暂停」")
        self.assertEqual(db.selection()["picked"], 0, "勾选应清空")
        self.assertEqual(db.selection()["total"], 1, "任务记录保留")

    def test_stop_button_available_while_paused(self):
        """暂停时 running 已经是 False，只判断 running 会让「停止」变灰。

        前端条件是 `!(running || paused)`，这里锁住后端的 paused 语义，
        防止以后有人把 paused 改成和 running 同义，又把这条出口堵上。
        """
        write_files({"a.bin": 100})
        engine, _ = make_engine({})
        engine.scan()

        engine.pause()
        time.sleep(0.3)
        self.assertFalse(engine.snapshot()["running"])
        self.assertTrue(engine.snapshot()["paused"], "暂停时必须能区分出 paused，否则前端无从判断")


class PickFilesTest(IsolatedCase):
    """「加入清单」的入口语义：勾文件加一个，勾目录递归展开。

    QA 报告问题 1 的根源在**前端**只把文件放进勾选集（按钮却叫「全选」），
    这里锁住后端「传了目录就该展开」这条契约，避免以后两边都不认账。
    """

    def setUp(self):
        import tempfile
        from pathlib import Path

        from panmigrate import config
        from panmigrate.server import App

        reset_demo()
        config.save({
            "source": {"provider": "mock", "auth_mode": "none", "root_path": "/"},
            "target": {"provider": "mock", "auth_mode": "none", "root_path": "/"},
        })
        folder = tempfile.mkdtemp(prefix="pmpick-")
        self.app = App(db_path=Path(folder) / "tasks.db")
        self.app.scheduler.stop()
        self.db = self.app.db

    def tearDown(self):
        self.app.scheduler.stop()
        self.app.db.close()
        super().tearDown()

    def test_dir_expands_recursively(self):
        from tests.test_server import item

        write_files({
            "照片/a.jpg": 100,
            "照片/2024/b.jpg": 120,
            "照片/2024/春节/c.jpg": 140,
        })
        res = self.app.pick_files([item("/照片", is_dir=True)])
        self.assertEqual(res["count"], 3, "三层嵌套应该全部展开")
        self.assertEqual(self.db.selection()["total"], 3)

    def test_same_file_twice_is_idempotent(self):
        from tests.test_server import item

        write_files({"a.txt": 100})
        self.app.pick_files([item("/a.txt")])
        first = self.db.selection()["total"]
        self.app.pick_files([item("/a.txt")])
        self.assertEqual(self.db.selection()["total"], first, "重复加入不该产生重复任务")

    def test_empty_dir_raises_and_leaves_nothing(self):
        from tests.test_server import item

        (DEMO_ROOT / "source" / "空空").mkdir(parents=True, exist_ok=True)
        with self.assertRaises(RuntimeError):
            self.app.pick_files([item("/空空", is_dir=True)])
        self.assertEqual(self.db.selection()["total"], 0, "空目录不该留下垃圾任务")
