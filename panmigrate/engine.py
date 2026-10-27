"""传输引擎。

核心是一件事：把源端的字节流，按分片并发地灌进目标端，全程不落整盘。

    scan()   → 递归扫源端，与目标端比对，生成迁移清单（增量）
    start()  → 多文件并发 × 单文件多分片并发，边下边传
    pause()  → 停在分片边界，进度已落盘，下次接着传

关键设计取舍：
* 分片放在内存（默认 8MB × 并发 4 ≈ 32MB），不写临时文件；
* upload_id 与分片地址一并存库，进程重启后先问服务端"哪些分片已经收过了"，
  只补差额，避免整文件重传；
* 校验用"大小 + 首/中/尾抽样比对"，而不是全文件 SHA1 —— 后者要额外读一遍
  源端，等于把流量翻倍，对网盘迁移不划算。
"""
from __future__ import annotations

import hashlib
import os
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .adapters.base import AdapterError, ReadCancelled, RemoteItem, UploadSession

MAX_PARTS = 1000
HARD_MAX_PARTS = 10000  # 底层网盘（阿里云 OSS 系）分片数的硬上限，超出必失败
MIN_PART = 256 * 1024
SAMPLE_BYTES = 64 * 1024

_LOG_PATH: Path | None = None


def _log_file() -> Path:
    """引擎日志落盘位置：data/panmigrate.log。

    用惰性初始化 + 全局缓存，避免每次 log 都去解析 config（config 里 DATA_DIR
    依赖 sys.frozen 判断，打包前后路径不同）。放 engine 这一层用 from . import
    config 会循环引用，所以这里直接用 config 的 DATA_DIR。
    """
    global _LOG_PATH
    if _LOG_PATH is None:
        from . import config

        _LOG_PATH = config.DATA_DIR / "panmigrate.log"
        _LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
    return _LOG_PATH


class VerificationError(Exception):
    pass


class TransferPaused(Exception):
    """用户暂停/中断，用来把任务干净地停在分片边界。"""


class ScanCancelled(Exception):
    """用户主动中断扫描（目录太大时用）。"""


def calc_parts(size: int, part_size_mb: int) -> tuple[int, int]:
    base = max(1, int(part_size_mb or 10)) * 1024 * 1024
    if size <= 0:
        return base, 1
    parts = (size + base - 1) // base
    part_size = base
    while parts > MAX_PARTS:
        part_size *= 2
        parts = (size + part_size - 1) // part_size
    part_size = max(MIN_PART, (part_size + MIN_PART - 1) // MIN_PART * MIN_PART)
    return part_size, max(1, (size + part_size - 1) // part_size)


class TransferEngine:
    def __init__(self, db, settings: dict | None = None):
        self.db = db
        self.settings = dict(settings or {})
        self.source = None
        self.sink = None
        self.source_root = "/"
        self.target_root = "/"

        self._lock = threading.RLock()
        # 目录创建必须串行：多个 worker 同时进同一个新目录时，
        # 目标端「同名同目录 refuse」会直接把其中一个任务判失败。
        self._dir_lock = threading.RLock()
        self._running = False
        self._stop = threading.Event()
        self._queue: queue.Queue = queue.Queue()
        self._workers: list[threading.Thread] = []

        # 已读但还没写进任务库的字节（进度 = db.done_bytes + 它）
        self._bytes_live = 0
        # 本轮读过的总字节，只增不减，仅供速度采样
        self._run_bytes = 0
        self._samples: deque = deque(maxlen=30)
        self._rate_lock = threading.Lock()
        self._rate_start = 0.0
        self._rate_bytes = 0
        self._run_started = 0.0
        self._run_total_bytes = 0
        self._scan_state: dict = {}
        self._scan_cancel = threading.Event()
        self._logs: deque = deque(maxlen=200)
        # 遇到「重试也没用」的错误（没登录 / 无权限）时记下原因并中止整轮
        self._fatal = ""

        # 上次是被强杀 / 直接关窗口的话，任务会留在 running：
        # 既不会被重新排队，界面还一直显示「正在传」。启动时复位一次，
        # 已传分片保留，用户点「开始迁移」就能接着续传。
        try:
            stuck = self.db.reset_running()
            if stuck:
                self.log(
                    f"上次有 {stuck} 个任务停在「传输中」（多半是直接关了程序），"
                    "已退回待处理，点「开始迁移」会接着传，已传分片不会重来",
                    "warn",
                )
        except Exception:
            pass

    # ---------------- 装配 ----------------
    @property
    def running(self) -> bool:
        return self._running

    def attach(self, source, sink, source_root: str, target_root: str) -> None:
        self.source = source
        self.sink = sink
        self.source_root = _norm(source_root)
        self.target_root = _norm(target_root)
        # 把停止信号交给源端：暂停时它能立刻中断正在读的分片，
        # 而不是傻等一整片（10MB 限速下几十秒）读完才肯退出。
        try:
            source.stop_event = self._stop
        except Exception:
            pass
        self.db.set_meta("source_root", self.source_root)
        self.db.set_meta("target_root", self.target_root)

    def log(self, message: str, level: str = "info") -> None:
        stamp = time.strftime("%H:%M:%S")
        self._logs.append({"t": stamp, "level": level, "msg": message})
        # 同时落盘到 data/panmigrate.log，方便用户排查「日志里出现的报错」。
        # 用追加模式写，文件过大时截掉旧的一半，避免无限膨胀。
        try:
            fp = _log_file()
            if fp.exists() and fp.stat().st_size > 400_000:
                tail = fp.read_text(encoding="utf-8", errors="replace")[-200_000:]
                fp.write_text(tail, encoding="utf-8")
            with fp.open("a", encoding="utf-8") as fh:
                fh.write(f"[{stamp}] [{level}] {message}\n")
        except OSError:
            pass

    # ---------------- 扫描 ----------------
    def scan(self) -> dict:
        if not self.source or not self.sink:
            raise AdapterError("还没有选择源和目标的网盘")
        settings = self.settings
        part_size_mb = int(settings.get("part_size_mb") or 10)

        self._scan_cancel.clear()
        self._scan_state = {"phase": "扫描源端", "files": 0, "bytes": 0, "current": ""}
        files: list[dict] = []

        def on_progress(rel: str, size: int) -> None:
            if self._scan_cancel.is_set():
                raise ScanCancelled("扫描已取消")
            self._scan_state["files"] += 1
            self._scan_state["bytes"] += size
            self._scan_state["current"] = rel

        unsupported = 0
        for item in self.source.walk_files(self.source_root, on_progress=on_progress):
            rel = item.path.lstrip("/")
            if not rel:
                continue
            part_size, total_parts = calc_parts(item.size, part_size_mb)
            # 扫描阶段就把「这条链路上注定传不了」的文件标出来（空文件、名字超长等）。
            # 否则要等传到它才失败，还白耗 retries × 退避时间。
            reason = ""
            if self.sink is not None:
                try:
                    reason = self.sink.reject_reason(item.name, item.size) or ""
                except Exception:
                    reason = ""
            if reason:
                unsupported += 1
            files.append(
                {
                    "rel_path": rel,
                    "name": item.name,
                    "src_id": item.id,
                    "size": item.size,
                    # 各网盘预上传要的摘要不同：123 要 MD5（etag），115 要 SHA1。
                    # 源端列表里有就都带上（还能触发秒传）
                    "md5": item.md5 or "",
                    "sha1": item.sha1 or "",
                    "part_size": part_size,
                    "total_parts": total_parts,
                    "skip": bool(reason),
                    "skip_reason": reason,
                }
            )

        self._scan_state["phase"] = "比对目标端"
        self._scan_state["current"] = ""
        try:
            existing = self.sink.walk_files(self.target_root) or {}
        except Exception as exc:  # 目标目录不存在等情况
            self.log(f"读取目标端失败，按空目录处理：{exc}", "warn")
            existing = {}

        skipped = 0
        for entry in files:
            if entry["skip"]:
                continue          # 已经因「不支持」跳过，不用再看目标端有没有
            hit = existing.get(entry["rel_path"])
            if hit and int(hit[0]) == entry["size"]:
                entry["skip"] = True
                skipped += 1

        self._scan_state["phase"] = "写入任务库"
        stats = self.db.upsert_plan(files)
        self._scan_state["phase"] = "完成"
        self._scan_state["unsupported"] = unsupported
        msg = (
            f"扫描完成：{len(files)} 个文件，{skipped} 个已存在可跳过；"
            f"新增 {stats['added']}、变更 {stats['updated']}、保留 {stats['kept']}"
        )
        if unsupported:
            msg += f"；{unsupported} 个目标网盘不支持，已自动跳过"
        self.log(msg, "warn" if unsupported else "info")
        return {
            "files": len(files),
            "skip": skipped,
            "unsupported": unsupported,
            "bytes": sum(f["size"] for f in files),
            **stats,
            "summary": self.db.summary(),
        }

    def scan_state(self) -> dict:
        return dict(self._scan_state)

    def cancel_scan(self) -> dict:
        self._scan_cancel.set()
        return {"ok": True}

    # ---------------- 运行 ----------------
    def start(self) -> dict:
        with self._lock:
            if self._running:
                # 上一轮的 worker 可能早就退出了，但收尾线程还没把标志复位
                # （比如卡在某个阻塞调用上）。这种情况下直接接管 ——
                # 否则用户会一直被「已经在跑了」顶回来，按钮一直灰着点不动。
                alive = [w for w in self._workers if w.is_alive()]
                if alive:
                    return {"ok": False, "msg": "还在收尾上一个分片，稍等一下再试",
                        "error": "还在收尾上一个分片，稍等一下再试"}
                self.log("上一轮其实已结束，状态没及时复位，已自动接管", "warn")
                self._running = False
            ids = self.db.pending_ids()
            if not ids:
                return {"ok": False, "msg": "没有待迁移的任务，先勾选文件或点「扫描整个目录」",
                        "error": "没有待迁移的任务，先勾选文件或点「扫描整个目录」"}
            self._running = True
            self._fatal = ""
            self._stop.clear()
            self._queue = queue.Queue()
            for task_id in ids:
                self._queue.put(task_id)

            self._bytes_live = 0
            self._run_bytes = 0
            self._samples.clear()
            with self._rate_lock:
                self._rate_start = time.time()
                self._rate_bytes = 0
            self._run_started = time.time()
            summary = self.db.summary()
            self._run_total_bytes = max(
                0, summary["total_bytes"] - summary["done_file_bytes"]
            )

            workers = max(1, int(self.settings.get("file_concurrency") or 2))
            # 源端有并发上限时（百度非会员），同时跑太多文件只会互相抢那两条连接，
            # 每个文件都推进得很慢。按源端能力收敛。
            limit = int(getattr(self.source, "MAX_CONCURRENT", 0) or 0)
            if limit:
                workers = min(workers, limit)
            self._workers = [
                threading.Thread(target=self._worker, name=f"mig-{i}", daemon=True)
                for i in range(workers)
            ]
            for worker in self._workers:
                worker.start()

            threading.Thread(target=self._watch, name="mig-watch", daemon=True).start()

        self.log(f"开始迁移：{len(ids)} 个文件，{workers} 个并发通道")
        return {"ok": True, "tasks": len(ids)}

    def pause(self) -> dict:
        self._stop.set()
        self.log("已请求暂停，正在收尾当前分片…", "warn")
        return {"ok": True}

    def cancel_pause(self) -> None:
        """清掉暂停标志，回到「空闲」。

        「停止」在已暂停时调用：此时 _running 已经是 False，只调 pause() 不会
        清掉 _stop，界面会一直停在「已暂停」——明明任务已经停了。
        """
        self._stop.clear()

    def reset(self) -> dict:
        self.pause()
        time.sleep(0.4)
        self.db.clear()
        self._bytes_live = 0
        self._run_bytes = 0
        self._run_total_bytes = 0
        self._samples.clear()
        self.log("已清空任务库")
        return {"ok": True}

    def _watch(self) -> None:
        for worker in self._workers:
            worker.join()
        with self._lock:
            self._running = False
        self._samples.clear()
        summary = self.db.summary()
        tail = f"（已因「{self._fatal}」中止，处理后再继续）" if self._fatal else ""
        self.log(
            f"本轮结束：成功 {summary['done']}、跳过 {summary['skipped']}、失败 {summary['failed']}{tail}",
            "warn" if (summary["failed"] or self._fatal) else "info",
        )

    # ---------------- worker ----------------
    def _worker(self) -> None:
        while not self._stop.is_set():
            if self._fatal:
                # 前面已经判定「整轮没法继续」，别再一个个去撞同一个墙
                return
            try:
                task_id = self._queue.get_nowait()
            except queue.Empty:
                return
            task = self.db.get(task_id)
            if not task or task["state"] in ("done", "skipped"):
                continue
            if self._stop.is_set():
                self.db.update(task_id, state="paused")
                continue
            self._process(task_id)

    def _process(self, task_id: int) -> None:
        task = self.db.get(task_id)
        if not task:
            return
        # 本轮重试次数与库里累计次数必须分开算。
        # 之前直接拿 attempts 当循环条件：上一轮攒到 retries 以上之后，
        # `while attempts <= retries` 一次都进不去，任务就永久卡在 failed，
        # 用户点「重试」也毫无反应 —— 这是个静默的死结。
        spent = int(task.get("attempts") or 0)
        retries = max(0, int(self.settings.get("retries") or 3))
        for round_no in range(retries + 1):
            if self._stop.is_set():
                self.db.update(task_id, state="paused")
                return
            self.db.update(task_id, state="running", error="")
            try:
                self._transfer(task)
                return
            except TransferPaused:
                self.db.update(task_id, state="paused")
                return
            except ReadCancelled:
                # 暂停时源端主动中断了读取（不是出错）：任务退回暂停，
                # 已传分片都保留，继续时接着传。
                self.db.update(task_id, state="paused")
                return
            except Exception as exc:
                message = _friendly(exc)
                # 登录态缺失这类错误重试一万次也不会变，只会让每个文件
                # 白等 2+4+8 秒。立即判失败、并且停掉整轮 ——
                # 后面的文件同样会因为没有登录而失败，继续跑没有意义。
                if not getattr(exc, "retryable", True):
                    self.db.update(task_id, state="failed", error=message)
                    self.log(f"中止：{task['rel_path']} —— {message}", "error")
                    if not self._fatal:
                        self._fatal = message
                        self.log(
                            "已停止本轮迁移：这是需要你手动处理的问题（多半是没登录），"
                            "处理好再点「开始迁移」即可，已传的分片会保留。",
                            "error",
                        )
                    self._stop.set()
                    return
                spent += 1
                self.db.update(task_id, attempts=spent)
                if round_no >= retries:
                    self.db.update(task_id, state="failed", error=message)
                    self.log(f"失败：{task['rel_path']} —— {message}", "error")
                    return
                wait = min(2 ** (round_no + 1), 20)
                self.log(
                    f"重试 {round_no + 1}/{retries}：{task['rel_path']} —— {message}（{wait}s 后）",
                    "warn",
                )
                if self._stop.wait(wait):
                    self.db.update(task_id, state="paused")
                    return
                task = self.db.get(task_id) or task

    # ---------------- 单个文件 ----------------
    def _transfer(self, task: dict) -> None:
        rel = task["rel_path"]
        name = task["name"]
        size = int(task["size"])
        part_size = int(task["part_size"] or 0) or calc_parts(size, 10)[0]
        total_parts = int(task["total_parts"] or 0) or max(1, (size + part_size - 1) // part_size)

        parent_rel = rel.rsplit("/", 1)[0] if "/" in rel else ""
        # 平铺模式：不管源端目录多深，全部落到目标目录根上
        flat = str(self.settings.get("structure") or "keep") == "flat"
        if parent_rel and not flat:
            parent_path = self.target_root.rstrip("/") + "/" + parent_rel
        else:
            parent_path = self.target_root
        conflict = str(self.settings.get("conflict") or "auto_rename").lower()
        name_mode = "auto_rename" if conflict == "auto_rename" else "refuse"

        # 建目录 + 查同名文件串行做：并发进同一层新目录时，第二个 worker
        # 会撞上「目录已存在（refuse）」而不是复用，整任务算失败。
        with self._dir_lock:
            parent_id = self.sink.ensure_dir(parent_path)
            existing = None
            if not task.get("upload_id"):
                existing = self.sink.child(parent_id, name)

        # 增量：目标端已有同大小同名文件，且没有半截的上传会话 → 跳过
        if not task.get("upload_id"):
            if existing and existing.size == size:
                self.db.update(
                    task["id"], state="skipped", done_bytes=size, parent_id=parent_id
                )
                self.log(f"跳过（目标已存在同名同大小文件）：{rel}")
                return
            if existing and conflict == "skip":
                self.db.update(
                    task["id"], state="skipped", parent_id=parent_id,
                    error="目标端已有同名文件但大小不同，按设置跳过",
                )
                self.log(f"跳过（同名不同大小）：{rel}", "warn")
                return

        item = RemoteItem(
            id=str(task.get("src_id") or ""),
            name=name,
            is_dir=False,
            path="/" + rel,
            size=size,
        )

        # 目标端要 SHA1（115），但源端（123/百度）列表里只有 MD5 —— 两者不可互推，
        # 只能把源文件完整读一遍算出来。算完落库，续传和重试都不会重复算。
        # 代价是这一个文件要额外下载一次，日志里会明确提示。
        if getattr(self.sink, "needs_sha1", False) and not str(task.get("sha1") or ""):
            self.log(f"目标端要求 SHA1，正读一遍源文件计算（会多消耗一次下载）：{rel}")
            try:
                sha1_hex = self._compute_sha1(item, size)
            except ReadCancelled:
                self.db.update(task["id"], state="paused")
                return
            self.db.update(task["id"], sha1=sha1_hex)
            task = {**task, "sha1": sha1_hex}

        session, done_parts = self._open_session(
            task, parent_id, name, size, part_size, total_parts, name_mode
        )
        final_name = session.name or name
        # 适配器可以自己定分片大小（百度固定 4MB），以它给的为准切数据
        if session.part_size:
            part_size = int(session.part_size)
        if session.parts:
            total_parts = len(session.parts)

        # 分片数硬上限：底层网盘（阿里云 OSS 系）普遍要求分片不超过 10000 片。
        # 固定分片的网盘（百度/夸克 4MB、天翼 10MB）遇到超大文件时，分片数会
        # 直接爆表（如夸克 4MB×10000≈39GB 封顶），这里提前拦住给出明确提示，
        # 而不是传到最后一片才报「Too many parts」让人摸不着头脑。
        if total_parts > HARD_MAX_PARTS:
            raise AdapterError(
                f"文件太大，超出网盘分片数上限：{rel}（{size} 字节 → {total_parts} 片，"
                f"上限 {HARD_MAX_PARTS} 片）。目标端固定分片 {part_size // (1024 * 1024)}MB，"
                "单文件约 " + f"{HARD_MAX_PARTS * part_size // (1024 * 1024 * 1024)}GB 封顶，"
                "请先把该大文件拆小或跳过。"
            )

        # 服务端判定「这个文件已经存在」时不会给 upload_id —— 一个分片都不用传，
        # 直接按完成收下（后面的抽样校验还是会验一遍内容）。
        if getattr(session, "rapid", False) and not session.parts:
            self.db.update(
                task["id"],
                state="done",
                file_id=session.file_id,
                upload_id="",
                parts_json="[]",
                done_bytes=size,
                done_parts=list(range(1, total_parts + 1)),
                parent_id=parent_id,
                error="",
            )
            self.log(f"秒传命中（目标端已有该文件，0 流量）：{rel}")
            if self.settings.get("verify", True):
                self._verify(task, item, parent_id, final_name, size)
            return

        resumed = bool(done_parts)
        pending = [
            part
            for part in session.parts
            if int(part.get("part_number") or 0) not in done_parts
        ]
        if done_parts:
            self.log(f"续传：{rel} 已有 {len(done_parts)}/{total_parts} 个分片在服务端")

        if pending:
            try:
                self._upload_parts(task, item, session, pending, part_size, size)
            except Exception as exc:
                # 续传时旧的分片上传地址可能已过期，丢掉会话重来一次
                if not resumed or not _looks_stale(exc):
                    raise
                self.log(f"续传地址已失效，重建上传会话：{rel}", "warn")
                self.db.update(
                    task["id"],
                    upload_id="",
                    file_id="",
                    parts_json="[]",
                    done_parts="[]",
                    done_bytes=0,
                )
                session, done_parts = self._open_session(
                    {**task, "upload_id": "", "parts_json": []},
                    parent_id,
                    name,
                    size,
                    part_size,
                    total_parts,
                    name_mode,
                )
                final_name = session.name or name
                if session.part_size:
                    part_size = int(session.part_size)
                if session.parts:
                    total_parts = len(session.parts)
                pending = list(session.parts)
                self._upload_parts(task, item, session, pending, part_size, size)

        if self._stop.is_set():
            raise TransferPaused()
        file_id = self.sink.complete(session)
        self.db.update(
            task["id"],
            state="done",
            file_id=file_id,
            done_bytes=size,
            done_parts=list(range(1, total_parts + 1)),
            parent_id=parent_id,
            error="",
        )

        if self.settings.get("verify", True):
            self._verify(task, item, parent_id, final_name, size)

        if final_name != name:
            self.log(f"完成（重名，目标端存为 {final_name}）：{rel}")
        else:
            self.log(f"完成：{rel}")

    def _open_session(self, task, parent_id, name, size, part_size, total_parts, name_mode="refuse"):
        upload_id = task.get("upload_id") or ""
        parts = task.get("parts_json") or []
        if upload_id and parts:
            session = UploadSession(
                file_id=task.get("file_id") or "",
                upload_id=upload_id,
                parts=parts,
                part_size=part_size,
                size=size,
            )
            try:
                done = set(self.sink.list_uploaded(session))
                if done:
                    return session, done
            except Exception as exc:
                self.log(f"查询已上传分片失败，重新建立会话：{exc}", "warn")
        elif upload_id:
            session = UploadSession(
                file_id=task.get("file_id") or "",
                upload_id=upload_id,
                parts=[],
                part_size=part_size,
                size=size,
            )
            try:
                done = set(self.sink.list_uploaded(session))
                if done:
                    return session, done
            except Exception:
                pass

        # 源端不一定给 MD5（百度/天翼的列表就不返回），但 123 云盘的预上传
        # 必须带一个 etag。这里用「源 id + 大小 + 文件名」拼一个**稳定伪值**：
        #   - 同一文件每次算出来一样 → 重复传不会出岔子
        #   - 不同文件 seed 不同 → 不会互相撞车、也不会让别的网盘误命中秒传
        # 实测 123 只要求它是 32 位十六进制，并不校验内容 MD5。
        md5_for_sink = str(task.get("md5") or "")
        if not md5_for_sink:
            seed = f"{task.get('src_id')}|{size}|{task.get('name') or name}"
            md5_for_sink = hashlib.md5(seed.encode("utf-8")).hexdigest()

        # 115 要 SHA1、123 要 MD5，两者不能互推，各自取各自的
        sha1_for_sink = str(task.get("sha1") or "")
        session = self.sink.create_upload(
            parent_id, name, size, part_size, name_mode,
            md5=md5_for_sink, sha1=sha1_for_sink,
        )
        patch = {
            "upload_id": session.upload_id,
            "file_id": session.file_id,
            "parts_json": session.parts,
            "parent_id": parent_id,
        }
        if session.parts:
            # 适配器可能改过分片大小（百度固定 4MB），落库时以它为准
            patch["part_size"] = int(session.part_size or part_size)
            patch["total_parts"] = len(session.parts)
        self.db.update(task["id"], **patch)
        return session, set()

    def _upload_parts(self, task, item, session, pending, part_size, size):
        concurrency = max(1, int(self.settings.get("part_concurrency") or 2))
        # 源端可能自带并发上限（百度非会员总带宽约 100KB/s，开 6 条连接只会
        # 每条都慢到被掐断）。这里按源端能力收敛，别让线程白等信号量。
        limit = int(getattr(self.source, "MAX_CONCURRENT", 0) or 0)
        if limit:
            concurrency = min(concurrency, limit)

        # 目标端要求顺序上传时（阿里云盘 OSS sequential multipart），
        # 并发或跳号都会 400 PartNotSequential，只能一片一片按序传。
        sequential = bool(getattr(self.sink, "sequential_parts", False))
        if sequential:
            concurrency = 1
            pending = sorted(pending, key=lambda p: int(p.get("part_number") or 0))

        done_parts = {int(p.get("part_number")) for p in session.parts} - {
            int(p.get("part_number")) for p in pending
        }
        if sequential and done_parts:
            # 已完成的分片必须是 1..k 的连续前缀，否则缺口之后的分片会被服务端拒。
            # 历史并发会话可能留下空洞，这种会话对顺序上传已经不可用 —— 抛成
            # 「失效」让上层重建会话（_looks_stale 认得「失效」这个词）。
            done_sorted = sorted(done_parts)
            if done_sorted != list(range(1, len(done_sorted) + 1)):
                raise AdapterError(
                    f"上传会话的分片记录不连续（{done_sorted[:6]}…），"
                    "该网盘要求顺序上传，会话已失效，将重建后重传"
                )
        lock = threading.Lock()

        def run_part(part: dict) -> int:
            if self._stop.is_set():
                raise TransferPaused()
            number = int(part.get("part_number") or 0)
            offset = (number - 1) * part_size
            length = min(part_size, max(0, size - offset))

            # 下载途中就实时报进度：百度这类网盘一个分片要读好几分钟
            # （内部还要再切成 1MB 小段重试），只看「分片完成数」会让界面
            # 长时间停在 0%，像卡死了。这里读到多少就先记多少。
            #
            # 两个计数器含义必须分清，混用就会导致「已传 395MB / 总 263MB」这种
            # 超过 100% 的怪现象：
            #   _bytes_live —— 已读**但还没写进任务库**的字节。
            #                  计入进度时与 db 的 done_bytes 相加，所以一旦
            #                  add_bytes 落库就必须立刻扣掉，否则同一批字节被算两遍。
            #   _run_bytes  —— 本轮真正读过的字节，只增不减，专门给速度采样用
            #                  （它减下去会让速度出现负值）。
            buffered = {"n": 0}

            def on_progress(n: int) -> None:
                if not n:
                    return
                buffered["n"] += n
                with lock:
                    self._bytes_live += n
                    self._run_bytes += n
                    self._samples.append((time.time(), self._run_bytes))

            try:
                data = (
                    self.source.read_range(item, offset, length, on_progress=on_progress)
                    if length else b""
                )
                if len(data) != length:
                    raise VerificationError(
                        f"第 {number} 分片读取长度不符（期望 {length}，实际 {len(data)}）"
                    )
                self.sink.put_part(part["upload_url"], data, number)
            except Exception:
                # 任何一步失败（读取 / 长度不符 / 上传被拒）都要把这批「已读但没入库」
                # 的字节退掉：外层会重试本片，不退就会越算越多。
                with lock:
                    self._bytes_live -= buffered["n"]
                raise

            self._throttle(length)
            with lock:
                done_parts.add(number)
                # 这些字节马上就要 add_bytes 落库了，从「未入库」里扣掉，
                # 由 summary 的 done_bytes 来代表它们，避免重复计算。
                self._bytes_live -= buffered["n"]
                # 适配器没回调过的部分（如整片直读）在这里补进速度采样
                self._run_bytes += length - buffered["n"]
                self._samples.append((time.time(), self._run_bytes))
                self.db.add_bytes(task["id"], length)
                self.db.update(task["id"], done_parts=sorted(done_parts))
            return number

        pool = ThreadPoolExecutor(max_workers=concurrency)
        try:
            futures = [pool.submit(run_part, part) for part in pending]
            for future in as_completed(futures):
                future.result()
        finally:
            # 暂停时把还没开始的分片直接取消，只有正在传的那几片会跑完
            pool.shutdown(wait=False, cancel_futures=True)

    def _compute_sha1(self, item, size: int) -> str:
        """把源文件完整读一遍算 SHA1（分块，不整文件进内存）。

        只给「目标端强制要 SHA1、源端又不给」的情况用，算完由调用方落库。
        """
        h = hashlib.sha1()
        step = 8 * 1024 * 1024
        off = 0
        while off < size:
            if self._stop.is_set():
                raise ReadCancelled()
            want = min(step, size - off)
            chunk = self.source.read_range(item, off, want)
            if not chunk:
                break
            h.update(chunk)
            off += len(chunk)
        if off != size:
            raise AdapterError(f"计算 SHA1 时只读到 {off}/{size} 字节，源端可能提前断流")
        return h.hexdigest().upper()

    def _verify(self, task, item, parent_id, name, size) -> None:
        target = self.sink.get_file(parent_id, name)
        if target is None:
            raise VerificationError("上传后目标端找不到文件")
        if target.size != size:
            raise VerificationError(f"大小不符（源 {size} / 目标 {target.size}）")

        reader = getattr(self.sink, "read_range", None)
        if reader is None or size == 0:
            return
        sample = min(SAMPLE_BYTES, size)
        offsets = {0, max(0, size // 2 - sample // 2), max(0, size - sample)}
        for offset in sorted(offsets):
            length = min(sample, size - offset)
            if length <= 0:
                continue
            try:
                source_bytes = self.source.read_range(item, offset, length)
                target_bytes = reader(target, offset, length)
            except Exception as exc:
                # 抽样读不回来（比如目标端不支持按区间取）就降级成只比大小，
                # 不能因为"校验手段不可用"把已经传好的文件判成失败
                self.log(
                    f"跳过抽样校验（{type(exc).__name__}）：{task['rel_path']}，已按大小核对",
                    "warn",
                )
                return
            if source_bytes != target_bytes:
                raise VerificationError(f"抽样校验不一致（偏移 {offset}）")
        self.log(f"校验通过：{task['rel_path']}（大小 + 3 处抽样比对）")

    # ---------------- 限速 ----------------
    def _throttle(self, length: int) -> None:
        """全局带宽限制（跨文件、跨分片生效）。

        用「累计字节 / 目标速率」算应到的时刻，谁超跑谁等一会儿，
        比每个线程各自 sleep 更稳：并发数变了速率也不会跟着漂。
        等待走 self._stop，所以暂停时不会卡在限速里。
        """
        bps = max(0, int(self.settings.get("bandwidth_kbps") or 0)) * 1024
        if bps <= 0 or length <= 0:
            return
        with self._rate_lock:
            self._rate_bytes += length
            due = self._rate_start + self._rate_bytes / bps
        delay = due - time.time()
        if delay > 0:
            self._stop.wait(delay)

    # ---------------- 状态 ----------------
    def speed(self) -> float:
        if len(self._samples) < 2:
            return 0.0
        (t0, b0), (t1, b1) = self._samples[0], self._samples[-1]
        span = t1 - t0
        return (b1 - b0) / span if span > 0.05 else 0.0

    def snapshot(self, task_limit: int = 800) -> dict:
        summary = self.db.summary()
        total_bytes = int(summary["total_bytes"] or 0)
        # 已传字节 = 任务库里已落库的字节 + 当前已读但还没入库的字节。
        # 不能用 done_file_bytes（只统计 state=done 的整文件）：那样正在传的
        # 文件进度永远是 0；也不能把两者相加重叠 —— 之前就是这么算出
        # 「已传 395MB / 总 263MB」的。这里统一以 db.done_bytes 为基准。
        done_total = min(
            total_bytes,
            max(0, int(summary["done_bytes"] or 0) + max(0, self._bytes_live)),
        )
        remaining = max(0, total_bytes - done_total)
        speed = self.speed() if self._running else 0.0
        eta = int(remaining / speed) if speed > 50 * 1024 else None
        tasks = self.db.list_tasks(task_limit)
        return {
            "running": self._running,
            "paused": not self._running and self._stop.is_set(),
            # 用户已点「暂停」但 worker 还在收尾当前分片（百度慢速下可能要几十秒）。
            # 界面靠它提前解锁「换网盘/浏览目录」等按钮 —— 既然已经决定停，
            # 就没必要让用户干等分片收尾。
            "pause_requested": self._stop.is_set(),
            # 非空表示本轮被强制中止（如未登录），界面要把它醒目地显示出来
            "fatal": self._fatal,
            "source_root": self.source_root,
            "target_root": self.target_root,
            "speed": speed,
            "eta": eta,
            "elapsed": int(time.time() - self._run_started) if self._run_started else 0,
            "live_bytes": self._bytes_live,
            # 界面进度用这个：已落库字节 + 当前分片已读到的字节。
            # 只用 summary["done_bytes"] 的话，百度那种一大片要读几分钟的场景
            # 会长时间停在 0%，看起来像卡死；但也不能超过总量（上限已夹住）。
            "done_bytes": done_total,
            "remaining": remaining,
            "summary": summary,
            "scan": self.scan_state(),
            "tasks": tasks,
            # 正在传的文件（最多几条）。慢速网盘一个大分片要好几分钟，
            # 只给全局进度条的话用户看不出「到底在干嘛」，容易以为卡死了。
            "active": [
                {
                    "rel": t["rel_path"],
                    "done_parts": len(t.get("done_parts") or []),
                    "total_parts": int(t.get("total_parts") or 0),
                    "done_bytes": int(t.get("done_bytes") or 0),
                    "size": int(t.get("size") or 0),
                }
                for t in tasks
                if t.get("state") == "running"
            ][:5],
            "logs": list(self._logs)[-60:],
        }


def _norm(path: str) -> str:
    path = (path or "/").strip()
    if not path.startswith("/"):
        path = "/" + path
    return path.rstrip("/") or "/"


def _friendly(exc: Exception) -> str:
    if isinstance(exc, AdapterError):
        return str(exc)
    if isinstance(exc, VerificationError):
        return str(exc)
    if isinstance(exc, OSError):
        return f"网络或文件系统错误：{exc}"
    return f"{type(exc).__name__}: {exc}"


def _looks_stale(exc: Exception) -> bool:
    """判断是不是"上传地址过期/会话失效"这类可以靠重建会话解决的问题。"""
    status = getattr(exc, "status", None)
    if status in (401, 403, 404):
        return True
    text = str(exc).lower()
    return any(word in text for word in ("expire", "过期", "失效", "no such upload", "invalid"))
