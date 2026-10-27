"""任务库（SQLite）。

每个文件一条记录，分片进度、upload_id、已完成分片都落盘 —— 所以进程被杀掉
之后再启动，能接着上次的位置继续传，而不是从头再来。
"""
from __future__ import annotations

import json
import sqlite3
import threading
import time
from pathlib import Path

from .config import DATA_DIR

DB_PATH = DATA_DIR / "tasks.db"

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    rel_path    TEXT NOT NULL UNIQUE,
    name        TEXT NOT NULL DEFAULT '',
    src_id      TEXT NOT NULL DEFAULT '',
    size        INTEGER NOT NULL DEFAULT 0,
    state       TEXT NOT NULL DEFAULT 'pending',
    selected    INTEGER NOT NULL DEFAULT 1,
    picked      INTEGER NOT NULL DEFAULT 0,
    done_bytes  INTEGER NOT NULL DEFAULT 0,
    part_size   INTEGER NOT NULL DEFAULT 0,
    total_parts INTEGER NOT NULL DEFAULT 0,
    done_parts  TEXT NOT NULL DEFAULT '[]',
    parts_json  TEXT NOT NULL DEFAULT '[]',
    parent_id   TEXT NOT NULL DEFAULT '',
    upload_id   TEXT NOT NULL DEFAULT '',
    file_id     TEXT NOT NULL DEFAULT '',
    sha1        TEXT NOT NULL DEFAULT '',
    md5         TEXT NOT NULL DEFAULT '',
    error       TEXT NOT NULL DEFAULT '',
    attempts    INTEGER NOT NULL DEFAULT 0,
    updated_at  TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS meta (k TEXT PRIMARY KEY, v TEXT NOT NULL DEFAULT '');
CREATE INDEX IF NOT EXISTS idx_tasks_state ON tasks(state);
"""


def _now() -> str:
    return time.strftime("%Y-%m-%d %H:%M:%S")


class TaskDB:
    def __init__(self, path: Path | None = None):
        self.path = path or DB_PATH
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(str(self.path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {row["name"] for row in self._conn.execute("PRAGMA table_info(tasks)")}
            for column, ddl in (
                ("selected", "ALTER TABLE tasks ADD COLUMN selected INTEGER NOT NULL DEFAULT 1"),
                ("picked", "ALTER TABLE tasks ADD COLUMN picked INTEGER NOT NULL DEFAULT 0"),
                # 永久性跳过的原因（如「目标网盘不支持 0 字节文件」）。
                # 和 error 分开：error 表示「这次失败了但可以重试」，
                # skip_reason 表示「这个文件在这条链路上永远传不了」。
                ("skip_reason", "ALTER TABLE tasks ADD COLUMN skip_reason TEXT NOT NULL DEFAULT ''"),
                # 源端文件的整文件 MD5：123 云盘预上传必须带它当 etag
                ("md5", "ALTER TABLE tasks ADD COLUMN md5 TEXT NOT NULL DEFAULT ''"),
            ):
                if column not in cols:
                    self._conn.execute(ddl)
            self._conn.commit()

    # ---------- meta ----------
    def set_meta(self, key: str, value) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (key, json.dumps(value, ensure_ascii=False) if not isinstance(value, str) else value),
            )
            self._conn.commit()

    def get_meta(self, key: str, default=None):
        with self._lock:
            row = self._conn.execute("SELECT v FROM meta WHERE k=?", (key,)).fetchone()
        if not row:
            return default
        raw = row["v"]
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return raw

    # ---------- 计划 ----------
    def upsert_plan(self, files: list[dict]) -> dict:
        """files: [{rel_path, name, size, skip, skip_reason}]。返回统计。

        skip_reason 用来记录「为什么跳过」——比如目标网盘不支持 0 字节文件、
        文件名超长。扫描阶段就判定并落库，用户在开始迁移前就能看到，
        不用等传到一半才冒出 12 个失败。
        """
        stats = {"added": 0, "updated": 0, "kept": 0, "removed": 0}
        keep = set()
        with self._lock:
            existing = {
                row["rel_path"]: row
                for row in self._conn.execute("SELECT * FROM tasks").fetchall()
            }
            for item in files:
                rel = item["rel_path"]
                keep.add(rel)
                size = int(item.get("size") or 0)
                part_size = int(item.get("part_size") or 0)
                total_parts = int(item.get("total_parts") or 0)
                reason = str(item.get("skip_reason") or "")
                skipped = bool(item.get("skip")) or bool(reason)
                old = existing.get(rel)
                if old is None:
                    self._conn.execute(
                        """INSERT INTO tasks(rel_path,name,src_id,size,state,part_size,total_parts,
                                    skip_reason,md5,sha1,updated_at)
                           VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                        (
                            rel,
                            item.get("name") or rel.rsplit("/", 1)[-1],
                            str(item.get("src_id") or ""),
                            size,
                            "skipped" if skipped else "pending",
                            part_size,
                            total_parts,
                            reason,
                            str(item.get("md5") or ""),
                            str(item.get("sha1") or ""),
                            _now(),
                        ),
                    )
                    stats["added"] += 1
                elif old["size"] != size:
                    self._conn.execute(
                        """UPDATE tasks SET size=?, name=?, src_id=?, state=?, part_size=?, total_parts=?,
                           done_bytes=0, done_parts='[]', parts_json='[]', upload_id='', file_id='',
                           error='', skip_reason=?, md5=?, sha1=?, attempts=0, updated_at=? WHERE rel_path=?""",
                        (
                            size,
                            item.get("name") or rel.rsplit("/", 1)[-1],
                            str(item.get("src_id") or ""),
                            "skipped" if skipped else "pending",
                            part_size,
                            total_parts,
                            reason,
                            str(item.get("md5") or ""),
                            str(item.get("sha1") or ""),
                            _now(),
                            rel,
                        ),
                    )
                    stats["updated"] += 1
                elif reason:
                    # 「永久性」跳过：这次扫描发现它注定传不了（空文件 / 名字超长）。
                    # 注意只有带 reason 才算永久；单纯的 skip=True（目标端已存在）
                    # 仍走后面的 kept 分支，不能把已完成的任务改回 skipped。
                    if old["state"] != "skipped" or (old["skip_reason"] or "") != reason:
                        self._conn.execute(
                            "UPDATE tasks SET state='skipped', skip_reason=?, updated_at=? WHERE rel_path=?",
                            (reason, _now(), rel),
                        )
                        stats["updated"] += 1
                    else:
                        stats["kept"] += 1
                elif (not skipped) and old["state"] in ("done", "skipped") and not old["skip_reason"]:
                    # 只有「这次扫描发现目标端没有它」（skipped=False）才重排。
                    # 带 skip_reason 的跳过是「永久性」的，不该因为目标端没这个文件就重排。
                    self._conn.execute(
                        """UPDATE tasks SET state='pending', done_bytes=0, done_parts='[]',
                           parts_json='[]', upload_id='', file_id='', error='', attempts=0,
                           updated_at=? WHERE rel_path=?""",
                        (_now(), rel),
                    )
                    stats["updated"] += 1
                else:
                    stats["kept"] += 1
            # 只清理「这次扫描没看到、且不是用户手动勾选进来」的旧任务。
            # 否则用「加入清单」挑好的文件，会被一次子目录扫描悄悄删掉。
            for rel, row in existing.items():
                if (
                    rel not in keep
                    and row["state"] in ("pending", "skipped")
                    and not row["picked"]
                ):
                    self._conn.execute("DELETE FROM tasks WHERE rel_path=?", (rel,))
                    stats["removed"] += 1
            self._conn.commit()
        return stats

    def clear(self) -> None:
        with self._lock:
            self._conn.execute("DELETE FROM tasks")
            self._conn.commit()

    # ---------- 勾选 ----------
    def set_selected(self, ids: list[int], flag: bool) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET selected=? WHERE id IN ({marks})",
                [1 if flag else 0] + list(ids),
            )
            self._conn.commit()
            return cur.rowcount

    def set_selected_all(self, flag: bool, only_open: bool = False) -> int:
        with self._lock:
            sql = "UPDATE tasks SET selected=?"
            args: list = [1 if flag else 0]
            if only_open:
                sql += " WHERE state NOT IN ('done','skipped')"
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.rowcount

    def select_by_paths(self, rel_paths: list[str], flag: bool = True) -> int:
        if not rel_paths:
            return 0
        marks = ",".join("?" * len(rel_paths))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET selected=? WHERE rel_path IN ({marks})",
                [1 if flag else 0] + list(rel_paths),
            )
            self._conn.commit()
            return cur.rowcount

    def mark_picked(self, rel_paths: list[str]) -> int:
        """标记为「用户手动勾选进来的」，扫描时不会被当成过期任务清掉。"""
        if not rel_paths:
            return 0
        marks = ",".join("?" * len(rel_paths))
        with self._lock:
            cur = self._conn.execute(
                f"UPDATE tasks SET picked=1 WHERE rel_path IN ({marks})", list(rel_paths)
            )
            self._conn.commit()
            return cur.rowcount

    def reset_running(self) -> int:
        """把上次异常退出时残留的 running 任务退回 pending。

        进程被强杀、直接关窗口、或系统崩溃后，任务会永远停在 running：
        既不会被 pending_ids() 选中重新排队（点「开始迁移」提示「没有待迁移
        的任务」），界面上又一直显示「正在传 第 N/M 片」，看着像卡死。
        启动时复位一次即可，已传分片的记录保留，下次接着续传。
        """
        with self._lock:
            cur = self._conn.execute(
                "UPDATE tasks SET state='pending', updated_at=? WHERE state='running'",
                (_now(),),
            )
            self._conn.commit()
            return cur.rowcount

    def requeue(self, ids: list[int] | None = None) -> int:
        """把失败/暂停的任务重新排进队列。

        光把它们留在 pending 是不行的：attempts 是累计计数，累计值一旦超过
        retries，重试循环就再也不会进入 —— 表现就是「点了重试没反应」。
        这里把计数清零，并重新勾选，保证下一轮真的会跑。
        """
        with self._lock:
            sql = (
                "UPDATE tasks SET state='pending', attempts=0, error='', selected=1 "
                "WHERE state IN ('failed','paused')"
            )
            args: list = []
            if ids:
                marks = ",".join("?" * len(ids))
                sql += f" AND id IN ({marks})"
                args = [int(i) for i in ids]
            cur = self._conn.execute(sql, args)
            self._conn.commit()
            return cur.rowcount

    def delete_tasks(self, ids: list[int]) -> int:
        if not ids:
            return 0
        marks = ",".join("?" * len(ids))
        with self._lock:
            cur = self._conn.execute(
                f"DELETE FROM tasks WHERE id IN ({marks})", [int(i) for i in ids]
            )
            self._conn.commit()
            return cur.rowcount

    def selection(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                """SELECT COUNT(*) AS total,
                          SUM(CASE WHEN selected=1 THEN 1 ELSE 0 END) AS picked,
                          SUM(CASE WHEN selected=1 THEN size ELSE 0 END) AS picked_bytes
                   FROM tasks"""
            ).fetchone()
        return {
            "total": row["total"] or 0,
            "picked": row["picked"] or 0,
            "picked_bytes": row["picked_bytes"] or 0,
        }

    # ---------- 查询 ----------
    def pending_ids(self) -> list[int]:
        """待跑的任务：待传 / 上次被中断 / 暂停 / 失败重试。小文件优先，避免长尾。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT id FROM tasks
                   WHERE selected=1 AND state IN ('pending','running','failed','paused')
                   ORDER BY size ASC, id ASC"""
            ).fetchall()
        return [row["id"] for row in rows]

    def get(self, task_id: int) -> dict | None:
        with self._lock:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        if not row:
            return None
        item = dict(row)
        item["done_parts"] = _safe_json(item["done_parts"], [])
        item["parts_json"] = _safe_json(item["parts_json"], [])
        return item

    def list_tasks(self, limit: int = 800) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT * FROM tasks ORDER BY id ASC LIMIT ?", (limit,)
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["done_parts"] = _safe_json(item["done_parts"], [])
            item["parts_json"] = _safe_json(item["parts_json"], [])
            out.append(item)
        return out

    def report_rows(self) -> list[dict]:
        """导出报告用：全量任务（不受界面上限约束），按路径排序。"""
        with self._lock:
            rows = self._conn.execute(
                """SELECT rel_path, size, state, selected, done_bytes,
                          total_parts, done_parts, attempts, error, updated_at
                   FROM tasks ORDER BY rel_path ASC"""
            ).fetchall()
        out = []
        for row in rows:
            item = dict(row)
            item["done_parts_count"] = len(_safe_json(item.pop("done_parts"), []))
            out.append(item)
        return out

    def summary(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                """SELECT
                     COUNT(*) AS total,
                     COALESCE(SUM(size),0) AS total_bytes,
                     COALESCE(SUM(done_bytes),0) AS done_bytes,
                     SUM(CASE WHEN state='done' THEN 1 ELSE 0 END) AS done,
                     SUM(CASE WHEN state='skipped' THEN 1 ELSE 0 END) AS skipped,
                     SUM(CASE WHEN state='failed' THEN 1 ELSE 0 END) AS failed,
                     SUM(CASE WHEN state='running' THEN 1 ELSE 0 END) AS running,
                     SUM(CASE WHEN state='paused' THEN 1 ELSE 0 END) AS paused,
                     SUM(CASE WHEN state='pending' THEN 1 ELSE 0 END) AS pending,
                     SUM(CASE WHEN state='done' THEN size ELSE 0 END) AS done_file_bytes
                   FROM tasks WHERE selected=1"""
            ).fetchone()
        data = dict(row)
        for key in list(data.keys()):
            if data[key] is None:
                data[key] = 0
        return data

    # ---------- 写入 ----------
    def update(self, task_id: int, **fields) -> None:
        if not fields:
            return
        if "done_parts" in fields and not isinstance(fields["done_parts"], str):
            fields["done_parts"] = json.dumps(fields["done_parts"])
        if "parts_json" in fields and not isinstance(fields["parts_json"], str):
            fields["parts_json"] = json.dumps(fields["parts_json"], ensure_ascii=False)
        fields["updated_at"] = _now()
        cols = ", ".join(f"{key}=?" for key in fields)
        values = list(fields.values()) + [task_id]
        with self._lock:
            self._conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", values)
            self._conn.commit()

    def add_bytes(self, task_id: int, delta: int) -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE tasks SET done_bytes=done_bytes+?, updated_at=? WHERE id=?",
                (delta, _now(), task_id),
            )
            self._conn.commit()

    def close(self) -> None:
        with self._lock:
            self._conn.close()


def _safe_json(raw, fallback):
    try:
        return json.loads(raw) if raw else fallback
    except (json.JSONDecodeError, TypeError):
        return fallback
