"""测试装置：隔离数据目录、造样本文件、可注入故障的假适配器。"""
from __future__ import annotations

import hashlib
import os
import pathlib
import shutil
import tempfile
import threading
import time
import unittest

from panmigrate.config import DATA_DIR
from panmigrate.db import TaskDB
from panmigrate.engine import TransferEngine
from panmigrate.adapters.mock import DEMO_ROOT, MockSink, MockSource

# 本次用例里建过的任务库，用完统一关掉（否则退出时会刷一堆 ResourceWarning）
_OPEN_DBS: list[TaskDB] = []


class IsolatedCase(unittest.TestCase):
    """用完自动关闭临时任务库的基类。"""

    def tearDown(self):
        while _OPEN_DBS:
            try:
                _OPEN_DBS.pop().close()
            except Exception:
                pass
        super().tearDown()


# ---------------- 目录 ----------------
def reset_demo() -> None:
    """清空演示盘两端，保证每个用例从零开始（否则会被增量逻辑跳过）。"""
    shutil.rmtree(DEMO_ROOT, ignore_errors=True)
    for sub in ("source", "target", ".parts"):
        (DEMO_ROOT / sub).mkdir(parents=True, exist_ok=True)


def write_files(spec: dict) -> dict:
    """在演示源盘造文件。spec: {相对路径: 字节数}。返回 {相对路径: bytes}。"""
    root = DEMO_ROOT / "source"
    made = {}
    for rel, size in spec.items():
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        data = os.urandom(size) if size else b""
        path.write_bytes(data)
        made[rel] = data
    return made


def target_path(rel: str) -> pathlib.Path:
    return DEMO_ROOT / "target" / rel


def sha1_bytes(data: bytes) -> str:
    return hashlib.sha1(data).hexdigest()


def sha1_file(path) -> str:
    return sha1_bytes(pathlib.Path(path).read_bytes())


def temp_db(name: str = "tasks") -> TaskDB:
    folder = tempfile.mkdtemp(prefix=f"pmdb-{name}-")
    db = TaskDB(pathlib.Path(folder) / "tasks.db")
    _OPEN_DBS.append(db)
    return db


# ---------------- 假适配器 ----------------
class RaceProbeSink(MockSink):
    """探测「同一目录被并发创建」：真的并发进来了就记一笔。

    引擎给建目录加了串行锁，所以 overlap 应该永远是 False。
    去掉那把锁，这个用例会立刻变红 —— 这就是它存在的意义。
    """

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg or {"mock_kbps": 0})
        self._lock = threading.Lock()
        self._active = 0
        self.overlap = False
        self.calls = 0

    def ensure_dir(self, path: str) -> str:
        with self._lock:
            self._active += 1
            self.calls += 1
            if self._active > 1:
                self.overlap = True
        try:
            time.sleep(0.03)  # 放大竞争窗口
            return super().ensure_dir(path)
        finally:
            with self._lock:
                self._active -= 1


class CorruptSink(MockSink):
    """把第 1 个分片改写一个字节，用来验证抽样校验能不能抓到静默损坏。"""

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg or {"mock_kbps": 0})

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        if part_number == 1 and data:
            data = bytes([data[0] ^ 0xFF]) + data[1:]
        return super().put_part(upload_url, data, part_number)


class ProgressSource(MockSource):
    """会回调 on_progress 的源。

    本地演示源是「一次读完」，压根不调 on_progress，所以用它测不到
    「边读边报进度」这条路径上的记账 bug（百度/阿里都是走这条路的）。
    """

    chunk = 64 * 1024

    def read_range(self, item, offset, length, on_progress=None):
        data = super().read_range(item, offset, length)
        if on_progress:
            for i in range(0, len(data), self.chunk):
                on_progress(min(self.chunk, len(data) - i))
        return data


class FlakySink(MockSink):
    """第一次上传第 1 片必失败，之后正常 —— 用来验证重试真的会重跑。"""

    def __init__(self, cfg: dict | None = None):
        super().__init__(cfg or {"mock_kbps": 0})
        self.fail_once = True

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        if part_number == 1 and self.fail_once:
            self.fail_once = False
            raise IOError("模拟网络中断")
        return super().put_part(upload_url, data, part_number)


class RapidSink(MockSink):
    """模拟阿里云盘「服务端已存在该文件」：create 只回 file_id，不回 upload_id。

    真实场景是之前传过、或服务端去重命中；此时工具应该 0 流量收下，
    而不是报「预上传没有返回 upload_id」把文件判成失败。
    """

    def __init__(self, payload: bytes):
        super().__init__({"mock_kbps": 0})
        self.payload = payload
        self.put_calls = 0

    def create_upload(
        self, parent_id, name, size, part_size, name_mode="refuse", md5="", sha1=""
    ):
        from panmigrate.adapters.base import UploadSession

        final = self._local(parent_id) / name
        final.parent.mkdir(parents=True, exist_ok=True)
        final.write_bytes(self.payload)
        return UploadSession(
            file_id=str(final), upload_id="", parts=[],
            part_size=part_size, size=size, rapid=True, name=name,
        )

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        self.put_calls += 1
        return super().put_part(upload_url, data, part_number)


# ---------------- 引擎 ----------------
def make_engine(settings: dict | None = None, sink=None, source=None, db=None):
    cfg = {"file_concurrency": 2, "part_concurrency": 2, "part_size_mb": 1,
           "retries": 1, "verify": True}
    cfg.update(settings or {})
    database = db or temp_db()
    engine = TransferEngine(database, cfg)
    engine.attach(
        source or MockSource({"mock_kbps": 0}),
        sink or MockSink({"mock_kbps": 0}),
        "/",
        "/",
    )
    return engine, database


def run_to_end(engine, timeout: float = 40.0) -> None:
    deadline = time.time() + timeout
    while engine.running and time.time() < deadline:
        time.sleep(0.02)
    if engine.running:
        raise AssertionError("迁移在 %.0f 秒内没有结束" % timeout)


def temp_home_exists() -> bool:
    return DATA_DIR.exists()
