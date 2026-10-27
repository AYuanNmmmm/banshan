"""本地演示适配器：用两个文件夹模拟两端网盘。

存在的意义很实际 —— 没有真实账号也能把整条链路（扫描/分片/并发/断点续传/
完整性校验）跑通，先证明引擎是对的，再去对接真实接口。
"""
from __future__ import annotations

import hashlib
import os
import shutil
import time
import uuid
from pathlib import Path

from ..config import DATA_DIR
from .base import AdapterError, RemoteItem, SinkAdapter, SourceAdapter, UploadSession

DEMO_ROOT = DATA_DIR / "demo"


def source_root() -> Path:
    return DEMO_ROOT / "source"


def target_root() -> Path:
    return DEMO_ROOT / "target"


class MockSource(SourceAdapter):
    kind = "source"
    label = "本地演示"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.root = source_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.rate = int(self.cfg.get("mock_kbps") or 8192) * 1024  # 字节/秒

    def describe(self) -> dict:
        return {"provider": "mock", "authorized": True, "root": str(self.root), "account": "本机"}

    def _local(self, path: str) -> Path:
        rel = path.strip("/")
        return self.root / rel if rel else self.root

    def list_dir(self, path: str) -> list[RemoteItem]:
        here = self._local(path)
        if not here.is_dir():
            raise AdapterError(f"源目录不存在：{path}")
        items: list[RemoteItem] = []
        with os.scandir(here) as it:
            for entry in it:
                st = entry.stat()
                items.append(
                    RemoteItem(
                        id=str(entry.path),
                        name=entry.name,
                        is_dir=entry.is_dir(),
                        # path 必须填：前端的勾选、进入目录全靠它匹配行
                        path=(f"{path.rstrip('/')}/{entry.name}" if path != "/" else f"/{entry.name}"),
                        size=0 if entry.is_dir() else st.st_size,
                        modified=str(int(st.st_mtime)),
                    )
                )
        items.sort(key=lambda x: (not x.is_dir, x.name))
        return items

    def read_range(self, item: RemoteItem, offset: int, length: int, on_progress=None) -> bytes:
        with open(item.id, "rb") as fh:
            fh.seek(offset)
            data = fh.read(length)
        if self.rate > 0:
            time.sleep(len(data) / self.rate)
        return data


class MockSink(SinkAdapter):
    kind = "sink"
    label = "本地演示"

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.root = target_root()
        self.root.mkdir(parents=True, exist_ok=True)
        self.rate = int(self.cfg.get("mock_kbps") or 8192) * 1024
        self._tmp = DEMO_ROOT / ".parts"
        self._tmp.mkdir(parents=True, exist_ok=True)

    def describe(self) -> dict:
        return {"provider": "mock", "authorized": True, "root": str(self.root), "account": "本机"}

    def _local(self, path: str) -> Path:
        rel = path.strip("/")
        return self.root / rel if rel else self.root

    def ensure_dir(self, path: str) -> str:
        target = self._local(path)
        target.mkdir(parents=True, exist_ok=True)
        return "/" + path.strip("/")

    def child(self, parent_id: str, name: str) -> RemoteItem | None:
        target = self._local(parent_id) / name
        if not target.exists():
            return None
        st = target.stat()
        sha1 = ""
        if target.is_file() and st.st_size <= 64 * 1024 * 1024:
            sha1 = _sha1_file(target)
        return RemoteItem(
            id=str(target), name=name, is_dir=target.is_dir(), size=st.st_size, sha1=sha1
        )

    def walk_files(self, path: str) -> dict:
        base = self._local(path)
        out: dict[str, tuple[int, RemoteItem]] = {}
        if not base.is_dir():
            return out
        for dirpath, _dirnames, filenames in os.walk(base):
            for filename in filenames:
                full = Path(dirpath) / filename
                rel = str(full.relative_to(base)).replace("\\", "/")
                size = full.stat().st_size
                out[rel] = (size, RemoteItem(id=str(full), name=filename, is_dir=False, size=size))
        return out

    def create_upload(
        self,
        parent_id: str,
        name: str,
        size: int,
        part_size: int,
        name_mode: str = "refuse",
        md5: str = "",
        sha1: str = "",
    ) -> UploadSession:
        target_dir = self._local(parent_id)
        final_name = name
        if (target_dir / name).exists():
            if name_mode == "refuse":
                raise AdapterError(f"目标端已存在同名文件：{name}")
            if name_mode == "auto_rename":
                final_name = self._free_name(target_dir, name)
        session_dir = self._tmp / uuid.uuid4().hex
        session_dir.mkdir(parents=True, exist_ok=True)
        count = max(1, (size + part_size - 1) // part_size)
        parts = [
            {"part_number": n + 1, "upload_url": str(session_dir / f"p{n + 1}")}
            for n in range(count)
        ]
        final = target_dir / final_name
        return UploadSession(
            file_id=str(final),
            upload_id=session_dir.name,
            parts=parts,
            part_size=part_size,
            size=size,
            name=final_name,
        )

    @staticmethod
    def _free_name(target_dir: Path, name: str) -> str:
        """同名自动加序号：a.jpg -> a (1).jpg"""
        if "." in name:
            stem, ext = name.rsplit(".", 1)
            suffix = "." + ext
        else:
            stem, suffix = name, ""
        n = 1
        while True:
            candidate = f"{stem} ({n}){suffix}"
            if not (target_dir / candidate).exists():
                return candidate
            n += 1

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        with open(upload_url, "wb") as fh:
            fh.write(data)
        if self.rate > 0:
            time.sleep(len(data) / self.rate)
        return hashlib.md5(data).hexdigest()

    def list_uploaded(self, session: UploadSession) -> list[int]:
        done = []
        for part in session.parts:
            path = Path(part["upload_url"])
            if path.exists() and path.stat().st_size > 0:
                done.append(int(part["part_number"]))
        return done

    def complete(self, session: UploadSession) -> str:
        final = Path(session.file_id)
        final.parent.mkdir(parents=True, exist_ok=True)
        session_dir = self._tmp / session.upload_id
        with open(final, "wb") as out:
            for part in sorted(session.parts, key=lambda p: int(p["part_number"])):
                src = Path(part["upload_url"])
                if not src.exists():
                    raise AdapterError(f"分片缺失：{src.name}")
                with open(src, "rb") as fh:
                    shutil.copyfileobj(fh, out, 1024 * 1024)
        shutil.rmtree(session_dir, ignore_errors=True)
        return str(final)

    def get_file(self, parent_id: str, name: str) -> RemoteItem | None:
        return self.child(parent_id, name)

    def browse(self, path: str) -> list[RemoteItem]:
        here = self._local(path)
        if not here.is_dir():
            return []
        items: list[RemoteItem] = []
        with os.scandir(here) as it:
            for entry in it:
                st = entry.stat()
                items.append(
                    RemoteItem(
                        id=str(entry.path),
                        name=entry.name,
                        is_dir=entry.is_dir(),
                        path=(f"{path.rstrip('/')}/{entry.name}" if path != "/" else f"/{entry.name}"),
                        size=0 if entry.is_dir() else st.st_size,
                    )
                )
        items.sort(key=lambda x: (not x.is_dir, x.name))
        return items

    def read_range(self, item: RemoteItem, offset: int, length: int, on_progress=None) -> bytes:
        with open(item.id, "rb") as fh:
            fh.seek(offset)
            return fh.read(length)


def _sha1_file(path: Path) -> str:
    digest = hashlib.sha1()
    with open(path, "rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest().upper()


def seed_demo_files() -> list[str]:
    """生成一批演示文件（含空文件、小文件、跨分片的大文件、中文名、深层目录）。"""
    root = source_root()
    root.mkdir(parents=True, exist_ok=True)
    plan = [
        ("文档/季度报告.txt", 12 * 1024),
        ("文档/空文件.txt", 0),
        ("图片/风景.jpg", 3 * 1024 * 1024 + 137),
        ("视频/旅行记录.mp4", 21 * 1024 * 1024 + 999),
        ("备份/数据集.bin", 9 * 1024 * 1024),
        ("备份/深层/再深一层/说明.md", 512),
    ]
    created = []
    for rel, size in plan:
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if path.exists():
            created.append(rel)
            continue
        with open(path, "wb") as fh:
            if size == 0:
                pass
            else:
                block = os.urandom(64 * 1024)
                written = 0
                while written < size:
                    chunk = block[: min(len(block), size - written)]
                    fh.write(chunk)
                    written += len(chunk)
        created.append(rel)
    return created
