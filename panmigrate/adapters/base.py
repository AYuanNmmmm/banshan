"""适配器抽象层。

任何网盘只要实现 Source（读）和 Sink（写）两套接口，就能接进迁移引擎。
引擎不关心对面是百度、阿里还是本地目录 —— 这也是为什么 Cookie 模式和
官方 OAuth 模式可以共存：换掉适配器实现即可，引擎一行不改。
"""
from __future__ import annotations

import http.client
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Iterator, Optional


class _NoLimit:
    """不做限制的「信号量」，让 with 语句在所有网盘上写法一致。

    也要提供 acquire(timeout)/release：读取路径用带超时的 acquire，
    才能在等连接槽位时响应「暂停」，而不是干等别人传完。
    """

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def acquire(self, timeout=None) -> bool:
        return True

    def release(self) -> None:
        return None


def account_label(adapter, fetch) -> str:
    """取账号显示名：只取一次，成功失败都缓存。

    各家的昵称接口五花八门（百度 netdisk_name、夸克 nickname、115 是脱敏手机号…），
    但界面只关心「有名字就显示」。统一成一个惰性取值 + 缓存的辅助函数，
    避免 describe() 被频繁调用时反复打接口。
    """
    if not hasattr(adapter, "_acc_cache"):
        try:
            adapter._acc_cache = (fetch() or "").strip()
        except Exception:
            adapter._acc_cache = ""
    if adapter._acc_cache:
        return adapter._acc_cache
    return str((getattr(adapter, "cfg", {}) or {}).get("account_name") or "")


@dataclass
class RemoteItem:
    id: str
    name: str
    is_dir: bool
    path: str = ""
    size: int = 0
    sha1: str = ""
    md5: str = ""
    modified: str = ""

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "name": self.name,
            "is_dir": self.is_dir,
            "path": self.path,
            "size": self.size,
            "sha1": self.sha1,
            "md5": self.md5,
            "modified": self.modified,
        }


@dataclass
class UploadSession:
    file_id: str
    upload_id: str
    parts: list
    part_size: int
    size: int
    rapid: bool = False
    # 目标端最终文件名。开启「同名自动加序号」时可能和源文件名不同，
    # 校验必须按这个名字回读，否则会找不到文件。
    name: str = ""


class AdapterError(Exception):
    """适配器层的统一异常，带给用户看的中文信息。
    retryable 决定引擎要不要重试：
      True  —— 网络抖动、限流、临时 5xx，重试有意义
      False —— 登录态失效、没权限、文件被限制，重试只会浪费
               用户的时间（每个文件白等 2+4+8 秒），应立即停下让人处理
    """

    retryable = True


class AuthError(AdapterError):
    """登录态缺失/失效。这类错误重试一万次也没用，必须让用户去登录。

    引擎遇到它会立刻把当前任务判失败，并中断整轮 —— 因为后面的文件
    同样会因为没有登录而全部失败，继续跑没有意义。
    """

    retryable = False


class ReadCancelled(Exception):
    """用户点了暂停，读取被主动中断。

    不继承 AdapterError：它不是"错误"，不该被重试，也不该记成失败。
    引擎收到后把这个任务退回 paused，并让本轮尽快收尾 —— 否则用户暂停后
    要等当前整个分片（10MB、限速下可能几十秒）传完才能重新点「开始迁移」，
    中途点会被「已经在跑了」顶回来，看起来就像按钮卡死。
    """


class SourceAdapter:
    kind = "source"
    label = "源"

    # 引擎注入的停止信号。置位后各适配器的分段读取会尽快中断。
    stop_event = None

    # ---- 下载能力声明（各网盘按自己的接口特性覆盖）----
    # 单次 Range 请求最多要多少字节。超过会被网盘拒绝（百度实测 >4MB 直接
    # 403 error_code 31326），所以统一按段切。0 表示不额外限制。
    read_max_bytes = 4 * 1024 * 1024
    # 连接太慢时自动把段减半，最低到这么多，避免一次要太多被掐断。
    min_segment = 256 * 1024
    # 单次 HTTP 读取的 socket 超时（秒）
    read_timeout = 90
    # 该网盘**总共**允许多少条下载连接（跨文件共享）。0 表示不限制。
    # 注意这是总量：4 个文件 × 6 分片 = 24 个线程，但实际连接不会超过这个数。
    MAX_CONCURRENT = 0

    @property
    def _read_sem(self):
        """整实例共享的下载并发闸门（首次访问时按 MAX_CONCURRENT 建）。"""
        sem = getattr(self, "_read_sem_obj", None)
        if sem is None:
            limit = int(getattr(self, "MAX_CONCURRENT", 0) or 0)
            if limit > 0:
                sem = threading.Semaphore(limit)
            else:
                sem = _NoLimit()
            self._read_sem_obj = sem
        return sem

    def describe(self) -> dict:
        raise NotImplementedError

    def list_dir(self, path: str) -> list[RemoteItem]:
        raise NotImplementedError

    def walk_files(self, path: str, on_progress=None) -> Iterator[RemoteItem]:
        """递归产出所有文件（跳过目录），用于生成迁移清单。"""
        return self._walk(path, "", on_progress)

    def _walk(self, root: str, rel: str, on_progress) -> Iterator[RemoteItem]:
        current = root.rstrip("/") + ("/" + rel if rel else "")
        if not current.startswith("/"):
            current = "/" + current
        entries = self.list_dir(current)
        for entry in entries:
            child_rel = (rel + "/" + entry.name).lstrip("/")
            if entry.is_dir:
                yield from self._walk(root, child_rel, on_progress)
            else:
                entry.path = "/" + child_rel
                if on_progress:
                    on_progress(child_rel, entry.size)
                yield entry

    def _prepare_download(self, item: RemoteItem, force_refresh: bool = False) -> tuple[str, dict]:
        """返回 (下载直链, 请求头)。force_refresh 表示「上一轮的链接可能失效了」。

        各网盘自己实现：百度走 filemetas（有缓存，可强制刷新）；
        阿里/夸克/天翼走各自的直链接口。
        """
        raise NotImplementedError

    def read_range(self, item: RemoteItem, offset: int, length: int, on_progress=None) -> bytes:
        """按区间读源端字节 —— 走得是通用实现（分段 + 续读 + 进度回报）。

        子类一般不用覆写本方法，只实现 _prepare_download 即可。
        on_progress(n)：每读完 n 个字节回调一次，界面据此实时显示进度。
        """
        return self._resumable_read(item, offset, length, on_progress)

    # ---- 可中断读取（用户点暂停时尽快退出，别让人干等一整个分片）----
    def _cancelled(self) -> bool:
        ev = getattr(self, "stop_event", None)
        return ev is not None and ev.is_set()

    def _interruptible_sleep(self, seconds: float) -> None:
        ev = getattr(self, "stop_event", None)
        if ev is not None:
            if ev.wait(seconds):
                raise ReadCancelled()
        else:
            time.sleep(seconds)

    def _resumable_read(self, item, offset, length, on_progress=None) -> bytes:
        """按 read_max_bytes 切段循环读；每段内部再走断点续读。"""
        seg_limit = int(self.read_max_bytes or 0) or (4 * 1024 * 1024)
        buf = bytearray()
        pos = offset
        end = offset + length
        while pos < end:
            if self._cancelled():
                raise ReadCancelled()
            want = min(seg_limit, end - pos)
            got = self._fetch_segment(item, pos, want)
            buf += got
            pos += len(got)
            if on_progress and got:
                on_progress(len(got))
        return bytes(buf)

    def _fetch_segment(self, item, offset: int, length: int) -> bytes:
        """读一段：流式小块读，断了从已读位置续读，连续失败则把段减半。"""
        buf = bytearray()
        attempts = 0
        max_attempts = 8
        seg = length
        last_err = ""

        while len(buf) < length:
            if attempts >= max_attempts:
                raise AdapterError(
                    f"下载失败（重试 {max_attempts} 次仍未读完：{last_err}）。"
                    "可能是网络不稳定或网盘限速，建议把「档位」调到「稳妥」再试。"
                )
            if attempts:
                self._interruptible_sleep(min(0.5 * (2 ** (attempts - 1)), 4))

            start = offset + len(buf)
            want = min(length - len(buf), seg)
            url, base_headers = self._prepare_download(item, force_refresh=attempts > 0)
            headers = dict(base_headers or {})
            headers["Range"] = f"bytes={start}-{start + want - 1}"
            headers.setdefault("Accept-Encoding", "identity")
            req = urllib.request.Request(url, headers=headers)
            before = len(buf)
            try:
                # 等连接槽位时也要能被暂停打断，否则暂停可能要等别人整片传完
                while not self._read_sem.acquire(timeout=0.2):
                    if self._cancelled():
                        raise ReadCancelled()
                try:
                    with urllib.request.urlopen(req, timeout=self.read_timeout) as resp:
                        while len(buf) - before < want:
                            # 64KB 一次，限速下也就零点几秒，暂停几乎立刻生效
                            if self._cancelled():
                                raise ReadCancelled()
                            piece = resp.read(64 * 1024)
                            if not piece:
                                break
                            buf += piece
                finally:
                    self._read_sem.release()
            except ReadCancelled:
                raise
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = exc.read().decode("utf-8", "replace")[:300]
                except Exception:
                    pass
                last_err = f"HTTP {exc.code}：{body}"
                if exc.code in (401, 403, 416):
                    # 直链失配 / 一次要太多 / 区间越界：换链 + 段减半重试
                    seg = max(self.min_segment, seg // 2)
                    attempts += 1
                    continue
                raise AdapterError(f"下载失败（HTTP {exc.code}）：{body[:200]}") from None
            except (http.client.IncompleteRead, ConnectionError,
                    urllib.error.URLError, OSError) as exc:
                # 连接中断：已读到的字节留在 buf 里，下一轮从断点续读
                last_err = f"{type(exc).__name__}: {exc}"[:200]
                if len(buf) - before < want:
                    seg = max(self.min_segment, seg // 2)
                attempts += 1
                continue

            if len(buf) - before < want:
                # 服务端提前关闭，也算一次中断
                seg = max(self.min_segment, seg // 2)
                attempts += 1

        return bytes(buf)

    def close(self) -> None:
        pass


class SinkAdapter:
    kind = "sink"
    label = "目标"

    # ---- 写入能力声明（供扫描阶段预检，别等传到一半才失败）----
    # 是否支持 0 字节文件。百度/夸克/天翼的接口都会拒绝，事先标出来能省掉
    # 「每个空文件都重试 3 次再失败」的无谓等待。
    supports_empty_file = True
    # 文件名长度上限（字节，0 = 不限）。夸克实测超过 255 会被截断。
    max_name_bytes = 0
    # 单文件大小上限（字节，0 = 不限）。115 是整文件上传，只有 500MB。
    # 在扫描阶段就标成跳过，别等传到一半才失败。
    max_file_bytes = 0
    # 预上传是否强制要求整文件 SHA1。115 是这样，123 要的是 MD5。
    # 源端给不出时，引擎会自己把源文件读一遍算（只算一次，结果落库）。
    needs_sha1 = False
    # 分片是否必须严格按 1,2,3… 顺序上传。
    # 阿里云盘（OSS sequential multipart）就是：跳号或并发上传直接
    # 400 PartNotSequential「For sequential multipart upload, you must upload
    # or complete parts with sequential part number」。声明为 True 后，
    # 引擎会把该文件的上传并发压到 1 并按序提交，别再改回并发。
    sequential_parts = False

    # 超限时给用户的补充说明（为什么有上限）
    upload_limit_hint = ""

    def describe(self) -> dict:
        raise NotImplementedError

    def reject_reason(self, name: str, size: int) -> str:
        """扫描阶段判断这个文件在这条链路上是否注定传不了。

        返回空字符串表示可以传；返回非空则是**给用户看的跳过原因**。
        子类可覆盖补充自家限制（如文件名非法字符）。
        """
        if size <= 0 and not self.supports_empty_file:
            return f"{self.label}不支持 0 字节文件，已跳过"
        if self.max_name_bytes and len(name.encode("utf-8")) > self.max_name_bytes:
            return (
                f"文件名超过 {self.max_name_bytes} 字节，{self.label}会截断，已跳过"
            )
        if self.max_file_bytes and size > self.max_file_bytes:
            return (
                f"超过 {self.label} 单文件上限 "
                f"{self.max_file_bytes // 1024 // 1024}MB（{self.upload_limit_hint or '这个网盘不支持更大的文件'}），已跳过"
            )
        return ""

    def walk_files(self, path: str) -> dict:
        """返回 {相对路径: (size, item)}，用于增量比对（只读，不建目录）。"""
        raise NotImplementedError

    def ensure_dir(self, path: str) -> str:
        """逐级创建目录，返回最终目录的 id。"""
        raise NotImplementedError

    def child(self, parent_id: str, name: str) -> Optional[RemoteItem]:
        raise NotImplementedError

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
        """name_mode 决定「目标端已有同名文件」时怎么办：

        refuse      —— 报错（默认，最保守）
        auto_rename —— 服务端自动加序号，session.name 里回真实名字

        md5 是**源端文件的整文件 MD5**（源网盘列表接口一般会返回）。
        123 云盘的预上传必须带它（etag 字段），115 云盘则要 sha1。
        两个都由引擎从源端列表里带下来，用不到的网盘忽略。
        """
        raise NotImplementedError

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        raise NotImplementedError

    def list_uploaded(self, session: UploadSession) -> list[int]:
        return []

    def complete(self, session: UploadSession) -> str:
        raise NotImplementedError

    def get_file(self, parent_id: str, name: str) -> Optional[RemoteItem]:
        return self.child(parent_id, name)

    def browse(self, path: str) -> list[RemoteItem]:
        """只读列出某个路径下的内容，供界面上的目录选择器使用。"""
        raise NotImplementedError

    def read_range(self, item: RemoteItem, offset: int, length: int, on_progress=None) -> bytes:
        """回读目标端字节，用于抽样校验。不实现则跳过抽样。"""
        raise NotImplementedError

    def quota(self) -> Optional[dict]:
        """目标盘容量（字节）。返回 {total, used, free}；拿不到就返回 None。

        迁移前用它判断「剩余空间够不够」，避免传到一半空间不足。
        """
        return None

    def close(self) -> None:
        pass
