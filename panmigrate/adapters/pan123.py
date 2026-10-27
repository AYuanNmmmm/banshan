"""123 云盘适配器（网页版登录态，扫码获得）。

为什么是网页版而不是开放平台：
  用户不想去申请 client_id/secret 再等审核，希望「扫码就能用」。
  实测（2026-10-26 真机）证明网页版**没有反爬签名** —— 原先担心的 CRC32 +
  时间戳混淆那套不存在，只要 `Authorization: Bearer <authorToken>` 就够。

实测确认的接口（全部在 yun.123pan.cn）：
  列表    GET  /api/file/list/new?driveId=0&parentFileId=&limit=&next=
  详情    GET  /api/file/detail?fileId=
  直链    POST /api/file/download_info {fileId}
  建目录  POST /api/file/upload_request {driveId, fileName, parentFileId, type:1}
          → data.Info.FileId
  删除    POST /api/file/trash {operation:true, fileTrashInfoList:[{FileId}]}
  预上传  POST /api/file/upload_request {driveId, etag, fileName, parentFileId, size, type:0, duplicate}
          → {Reuse, FileId, Key, Bucket, UploadId, SliceSize, StorageNode, EndPoint}
  预签名  POST /api/file/s3_upload_object/auth          （单分片）
          POST /api/file/s3_repare_upload_parts_batch    （多分片，必须带 fileId）
          两者入参：{bucket, key, uploadId, StorageNode, partNumberStart, partNumberEnd}
          → {presignedUrls: {"1": url}, headers: {...}}
  合并    POST /api/file/upload_complete/v2
          {fileId, bucket, fileSize, key, isMultipart, uploadId, StorageNode}
          → data.file_info（有就成功；没有则轮询）
  轮询    GET  /api/file/upload_complete/result?<同上字段>

必须记住的坑（全是真机踩出来的，改代码前先读一遍）：
  1. **`file/list` 是废弃接口，永远返回空**；必须用 `file/list/new`。
     这个坑会让人误以为上传全失败 —— 其实是查错了接口看不到文件。
  2. **单分片和多分片走不同接口**：size ≤ SliceSize 用 `s3_upload_object/auth`，
     且不需要 fileId；多分片用 `s3_repare_upload_parts_batch`，**必须带 fileId**。
     单分片误用 batch 会返回「非法请求」。
  3. **`partNumberEnd` 是开区间**：2 片文件要传 3 才拿到 2 个 URL。取值 = 末片号 + 1。
  4. 两个预签名接口都要 `StorageNode`（**大写 S**）+ `bucket`/`key`/`uploadId`。
  5. 合并请求的字段是 `fileId`（小写 f）+ `fileSize` + `isMultipart` + `StorageNode`，
     少了任何一个都会静默返回 code=0 但文件不落地。
  6. 合并返回里 `data.file_info` 有值才算成功；没有就 GET `upload_complete/result`
     轮询（间隔取返回的 `duration` 秒）。
  7. 预上传的 `etag` 是**整文件 MD5**，必填。拿不到就无法上传（源端列表一般会带）。
  8. **秒传命中时 `Bucket` 是空字符串**、`Reuse=true`，此时不要再去预签名，
     直接当成功处理 —— 文件已经建好了。
  9. **同一个文件名/同一份内容反复 upload_request 会返回残缺会话**（Bucket 空但
     Reuse=false）。所以测试要用随机内容 + 唯一文件名，否则会误判成接口有问题。
"""
from __future__ import annotations

import base64
import json
import time
import urllib.parse
from typing import Iterator

from . import httpc
from .base import (
    AdapterError,
    AuthError,
    RemoteItem,
    SinkAdapter,
    SourceAdapter,
    UploadSession,
    account_label,
)
from .httpc import HttpError

API = "https://yun.123pan.cn"
ROOT_ID = "0"
APP_VERSION = "132"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 一次预签名最多要多少片。片数多时分批，避免单个响应过大
PART_BATCH = 100


class Pan123Error(AdapterError):
    pass


class Pan123AuthError(Pan123Error, AuthError):
    """登录态缺失/失效：重试无意义，要让用户重新扫码。"""

    retryable = False


class _Pan123Common:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.token = (self.cfg.get("access_token") or "").strip()
        self._children: dict[str, list[RemoteItem]] = {}
        self._path_ids: dict[str, str] = {"": ROOT_ID, "/": ROOT_ID}
        # 换直链要 fileId+size+s3keyFlag+fileName+etag 五件套，列表里没全带，
        # 这里按 fileId 把原始字段存下来备用
        self._meta: dict[str, dict] = {}
        # 直链缓存：换一次要三跳，同一文件连续读多段时别重复换
        self._url_cache: dict[str, tuple[str, float]] = {}

    def fetch_account(self) -> str:
        """123 的 Nickname 就是账号显示名（没设昵称时是手机号）。"""
        d = self._call("GET", "/api/user/info")
        return str(d.get("Nickname") or d.get("Passport") or "")

    def describe(self) -> dict:
        return {
            "provider": "pan123",
            "mode": "web_qr",
            "authorized": bool(self.token),
            "account": account_label(self, self.fetch_account),
            "scope_note": "网页扫码登录：可读写整个网盘（不走开放平台、不用申请密钥）",
        }

    def _headers(self) -> dict:
        return {
            "Authorization": f"Bearer {self.token}",
            "platform": "web",
            "App-Version": APP_VERSION,
            "User-Agent": UA,
            "Referer": f"{API}/",
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json;charset=UTF-8",
        }

    def ensure_auth(self) -> None:
        if not self.token:
            raise Pan123AuthError(
                "还没登录 123 云盘：点「扫码登录」用手机扫一下就行，不用填任何密钥"
            )

    def _call(
        self,
        method: str,
        path: str,
        body: dict | None = None,
        query: dict | None = None,
    ) -> dict:
        self.ensure_auth()
        url = f"{API}{path}"
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = json.dumps(body).encode("utf-8") if body is not None else None
        try:
            _, _, raw = httpc.request(
                method, url, headers=self._headers(), data=data, timeout=60, retries=2
            )
        except HttpError as exc:
            raise Pan123Error(
                f"123 云盘接口 {path} 失败（HTTP {exc.status}）：{exc.body[:200]}"
            ) from None
        try:
            resp = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise Pan123Error(
                f"123 云盘接口 {path} 返回非 JSON：{raw.decode('utf-8', 'replace')[:200]}"
            ) from None
        code = resp.get("code")
        if code not in (0, "0", None):
            message = resp.get("message") or resp.get("msg") or ""
            if code in (401, 403, 40101, 40102):
                raise Pan123AuthError(
                    f"123 云盘登录态已失效（code {code}）：{message}，请重新扫码登录"
                )
            raise Pan123Error(f"123 云盘返回错误 code {code}：{message}")
        return resp.get("data") or {}

    # ---------- 目录 ----------
    def list_folder(self, folder_id: str, force: bool = False) -> list[RemoteItem]:
        folder_id = str(folder_id or ROOT_ID)
        if not force and folder_id in self._children:
            return self._children[folder_id]
        items: list[RemoteItem] = []
        cursor = ""
        for _ in range(2000):
            # 必须 list/new：file/list 永远返回空
            query = {
                "driveId": 0,
                "parentFileId": folder_id,
                "limit": 100,
                "orderBy": "file_id",
                "orderDirection": "desc",
                "trashed": "false",
            }
            if cursor:
                query["next"] = cursor
            data = self._call("GET", "/api/file/list/new", query=query)
            batch = data.get("InfoList") or data.get("infoList") or []
            for raw in batch:
                fid = str(raw.get("FileId") or "")
                self._meta[fid] = {
                    "name": raw.get("FileName") or "",
                    "size": int(raw.get("Size") or 0),
                    "etag": raw.get("Etag") or "",
                    "s3keyFlag": raw.get("S3KeyFlag") or "",
                }
                items.append(
                    RemoteItem(
                        id=fid,
                        name=raw.get("FileName") or "",
                        is_dir=int(raw.get("Type") or 0) == 1,
                        size=int(raw.get("Size") or 0),
                        md5=(raw.get("Etag") or "").lower(),
                        modified=str(raw.get("UpdateAt") or raw.get("CreateAt") or ""),
                    )
                )
            nxt = data.get("Next")
            if nxt in (None, "", -1, "-1") or not batch:
                break
            cursor = str(nxt)
        self._children[folder_id] = items
        return items

    def resolve_path(self, path: str) -> str:
        path = "/" + (path or "/").strip().strip("/")
        if path == "/":
            return ROOT_ID
        if path in self._path_ids:
            return self._path_ids[path]
        current = ROOT_ID
        walked = ""
        for name in [p for p in path.split("/") if p]:
            walked += "/" + name
            if walked in self._path_ids:
                current = self._path_ids[walked]
                continue
            found = None
            for entry in self.list_folder(current):
                if entry.name == name and entry.is_dir:
                    found = entry
                    break
            if not found:
                raise Pan123Error(f"123 云盘目录不存在：{walked}")
            current = found.id
            self._path_ids[walked] = current
        return current

    def list_dir(self, path: str) -> list[RemoteItem]:
        path = "/" + (path or "/").strip().strip("/")
        folder_id = self.resolve_path(path)
        items = self.list_folder(folder_id)
        for entry in items:
            entry.path = (
                (path.rstrip("/") + "/" + entry.name) if path != "/" else "/" + entry.name
            )
        return items

    # ---------- 下载 ----------
    # 直链是**三跳**拿到的（真机实测，少一步都不行）：
    #   1. POST download_info（必须带 fileId+size+s3keyFlag+fileName+etag）
    #      → DownloadUrl，形如 https://web-pro2.../download-v2/?params=<base64>
    #   2. params 是 base64，解码后得到 CDN 中转地址；GET 它返回 210 + JSON
    #      → data.redirect_url
    #   3. redirect_url 才是真正能 Range 的 CDN 地址（206 + Content-Range）
    def download_url(self, file_id: str, force: bool = False) -> str:
        fid = str(file_id)
        now = time.time()
        if not force:
            hit = self._url_cache.get(fid)
            if hit and now - hit[1] < 120:
                return hit[0]

        meta = self._meta.get(fid)
        if not meta:
            raise Pan123Error(
                "123 云盘缺少这个文件的元信息（size/s3keyFlag/etag），"
                "请先刷新目录再试"
            )
        data = self._call("POST", "/api/file/download_info", {
            "fileId": int(fid),
            "size": meta["size"],
            "s3keyFlag": meta["s3keyFlag"],
            "fileName": meta["name"],
            "etag": meta["etag"],
        })
        raw = data.get("DownloadUrl") or data.get("downloadUrl") or data.get("url") or ""
        if not raw:
            raise Pan123Error(
                "123 云盘没有返回下载直链（免费账号当日流量可能已用完，"
                f"或没开直链空间）：{str(data)[:160]}"
            )

        final = raw
        # 第二跳：params 里是 base64 编码的中转地址
        if "params=" in raw:
            b64 = (urllib.parse.parse_qs(urllib.parse.urlparse(raw).query).get("params") or [""])[0]
            if b64:
                try:
                    mid = base64.b64decode(b64 + "=" * (-len(b64) % 4)).decode("utf-8")
                except Exception:
                    mid = ""
                if mid.startswith("http"):
                    # 第三跳：中转地址返回 210 + JSON，里面才是最终 CDN
                    try:
                        _, _, body = httpc.request(
                            "GET", mid,
                            headers={"User-Agent": UA, "Referer": f"{API}/"},
                            timeout=30, retries=1,
                        )
                        payload = json.loads(body.decode("utf-8", "replace"))
                        final = ((payload.get("data") or {}).get("redirect_url") or mid)
                    except Exception:
                        final = mid

        self._url_cache[fid] = (final, now)
        return final


class Pan123Source(_Pan123Common, SourceAdapter):
    kind = "source"
    label = "123 云盘"

    read_max_bytes = 4 * 1024 * 1024
    MAX_CONCURRENT = 4

    def walk_files(self, path: str, on_progress=None) -> Iterator[RemoteItem]:
        yield from self._walk("/" + (path or "").strip("/"), "", on_progress)

    def _prepare_download(self, item: RemoteItem, force_refresh: bool = False) -> tuple[str, dict]:
        return self.download_url(item.id, force=force_refresh), {
            "User-Agent": UA,
            "Referer": f"{API}/",
        }


class Pan123Sink(_Pan123Common, SinkAdapter):
    """123 云盘作为目标端。

    走 S3 分片：预上传拿会话 → 预签名拿 PUT 地址 → 逐片 PUT → 合并。
    分片大小由服务端 SliceSize 决定（16MB），所以要把它回填给引擎。
    """

    kind = "sink"
    label = "123 云盘"
    supports_empty_file = True
    max_name_bytes = 255
    # 分片可乱序并发上传
    sequential_parts = False

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        # uploadId → 完整会话信息（bucket/key/StorageNode/sliceSize/isMultipart/fileId）
        self._sessions: dict[str, dict] = {}

    # ---------- 目录 ----------
    def walk_files(self, path: str) -> dict:
        out: dict[str, tuple[int, RemoteItem]] = {}
        start = "/" + (path or "/").strip().strip("/")
        stack = [(start, "")]
        while stack:
            current, rel = stack.pop()
            try:
                entries = self.list_dir(current)
            except Pan123Error:
                continue
            for entry in entries:
                child_rel = (rel + "/" + entry.name).lstrip("/")
                if entry.is_dir:
                    stack.append((entry.path, child_rel))
                else:
                    out[child_rel] = (entry.size, entry)
        return out

    def browse(self, path: str) -> list[RemoteItem]:
        return self.list_dir(path)

    def ensure_dir(self, path: str) -> str:
        path = "/" + (path or "/").strip().strip("/")
        if path == "/":
            return ROOT_ID
        if path in self._path_ids:
            return self._path_ids[path]
        current = ROOT_ID
        walked = ""
        for name in [p for p in path.split("/") if p]:
            walked += "/" + name
            if walked in self._path_ids:
                current = self._path_ids[walked]
                continue
            found = None
            for entry in self.list_folder(current):
                if entry.name == name and entry.is_dir:
                    found = entry
                    break
            if found:
                current = found.id
            else:
                # 建目录 = upload_request 带 type:1，结果在 data.Info.FileId
                data = self._call("POST", "/api/file/upload_request", {
                    "driveId": 0,
                    "etag": "",
                    "size": 0,
                    "type": 1,
                    "duplicate": 1,
                    "RequestSource": None,
                    "fileName": name,
                    "parentFileId": int(current),
                })
                info = data.get("Info") or {}
                current = str(info.get("FileId") or "")
                if not current:
                    raise Pan123Error(f"123 云盘建目录没有返回 FileId：{str(data)[:200]}")
            self._path_ids[walked] = current
            self._children.pop(current, None)
        return current

    def child(self, parent_id: str, name: str) -> RemoteItem | None:
        for entry in self.list_folder(parent_id):
            if entry.name == name:
                return entry
        return None

    # ---------- 上传 ----------
    def _prepare_parts(self, meta: dict, total: int, is_one: bool) -> dict[str, str]:
        """拿全部分片的 PUT 地址。

        partNumberEnd 是**开区间**，所以末尾要传「末片号+1」。
        片数多时分批请求，避免单次响应过大。
        """
        urls: dict[str, str] = {}
        start = 1
        while start <= total:
            end = min(start + PART_BATCH - 1, total)
            body = {
                "bucket": meta["bucket"],
                "key": meta["key"],
                "uploadId": meta["uploadId"],
                "StorageNode": meta["storageNode"],
                "partNumberStart": start,
                "partNumberEnd": end + 1,   # 开区间
            }
            if not is_one:
                # 多分片必须带 fileId，否则报「非法请求」
                body["fileId"] = meta["fileId"]
            path = "/api/file/s3_upload_object/auth" if is_one else "/api/file/s3_repare_upload_parts_batch"
            data = self._call("POST", path, body)
            got = data.get("presignedUrls") or {}
            for k, v in got.items():
                urls[str(k)] = v
            start = end + 1
        return urls

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
        existing = self.child(parent_id, name)
        if existing is not None and name_mode == "refuse":
            raise Pan123Error(f"目标端已存在同名文件：{name}")
        # duplicate: 0 默认 / 1 保留两者 / 2 覆盖
        duplicate = 2 if (existing is not None and name_mode == "refuse") else (
            1 if existing is not None else 0
        )

        if not md5:
            # 123 的预上传必须带整文件 MD5（etag 字段），空值直接被拒
            raise Pan123Error(
                "源端没有提供这个文件的 MD5，123 云盘无法上传"
                "（它的预上传要求整文件 MD5）。换一个能提供 MD5 的源盘再试"
            )

        data = self._call("POST", "/api/file/upload_request", {
            "driveId": 0,
            "etag": md5.lower(),
            "fileName": name,
            "parentFileId": int(parent_id or ROOT_ID),
            "size": int(size),
            "type": 0,
            "duplicate": duplicate,
        })

        if data.get("Reuse"):
            # 秒传命中：文件已经建好了，不需要传任何字节
            info = data.get("Info") or {}
            return UploadSession(
                file_id=str(info.get("FileId") or data.get("FileId") or ""),
                upload_id="",
                parts=[],
                part_size=int(data.get("SliceSize") or 0) or part_size,
                size=size,
                rapid=True,
                name=name,
            )

        bucket = data.get("Bucket") or ""
        key = data.get("Key") or ""
        upload_id = data.get("UploadId") or ""
        storage_node = data.get("StorageNode") or ""
        if not (bucket and key and upload_id and storage_node):
            raise Pan123Error(f"123 云盘预上传返回不完整（缺少 bucket/key/uploadId）：{str(data)[:200]}")

        slice_size = int(data.get("SliceSize") or 0) or (16 * 1024 * 1024)
        total = max(1, (int(size) + slice_size - 1) // slice_size)
        is_one = total == 1
        meta = {
            "fileId": str(data.get("FileId") or ""),
            "bucket": bucket,
            "key": key,
            "uploadId": upload_id,
            "storageNode": storage_node,
            "sliceSize": slice_size,
            "isMultipart": not is_one,
            "name": name,
            "size": int(size),
        }
        urls = self._prepare_parts(meta, total, is_one)
        if len(urls) < total:
            raise Pan123Error(
                f"123 云盘只拿到 {len(urls)}/{total} 个分片地址，无法继续"
            )
        self._sessions[upload_id] = meta

        parts = [
            {"part_number": n, "upload_url": urls[str(n)]} for n in range(1, total + 1)
        ]
        return UploadSession(
            file_id=meta["fileId"],
            upload_id=upload_id,
            parts=parts,
            part_size=slice_size,
            size=size,
            name=name,
        )

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        # upload_url 就是预签名地址，直接 PUT；不要带 Authorization（会签名冲突）
        try:
            status, headers, _ = httpc.request(
                "PUT",
                upload_url,
                headers={"Content-Type": "application/octet-stream"},
                data=data,
                timeout=300,
                retries=2,
            )
        except HttpError as exc:
            raise Pan123Error(
                f"123 云盘第 {part_number} 片上传失败（HTTP {exc.status}）：{exc.body[:200]}"
            ) from None
        if status not in (200, 201):
            raise Pan123Error(f"123 云盘第 {part_number} 片上传返回 HTTP {status}")
        return (headers.get("ETag") or headers.get("Etag") or "").strip('"')

    def complete(self, session: UploadSession) -> str:
        if session.rapid:
            return session.file_id
        meta = self._sessions.get(session.upload_id)
        if not meta:
            raise Pan123Error("123 云盘上传会话已丢失，请重新开始这个文件")

        body = {
            "fileId": meta["fileId"],
            "bucket": meta["bucket"],
            "fileSize": meta["size"],
            "key": meta["key"],
            "isMultipart": meta["isMultipart"],
            "uploadId": meta["uploadId"],
            "StorageNode": meta["storageNode"],
        }
        data = self._call("POST", "/api/file/upload_complete/v2", body)
        info = data.get("file_info")
        if info:
            self._remember(info)
            self._sessions.pop(session.upload_id, None)
            self._children.clear()
            return str(info.get("FileId") or meta["fileId"])

        # 没直接完成 → 轮询结果（间隔取服务端给的 duration）
        for _ in range(60):
            poll = self._call("GET", "/api/file/upload_complete/result", query=body)
            info = poll.get("file_info")
            if info:
                self._remember(info)
                self._sessions.pop(session.upload_id, None)
                self._children.clear()
                return str(info.get("FileId") or meta["fileId"])
            time.sleep(float(poll.get("duration") or 2))
        raise Pan123Error("123 云盘合并分片超时（2 分钟内未完成），请重试这个文件")

    def _remember(self, info: dict) -> None:
        """把接口返回的文件信息记进 _meta，供换直链时凑齐五件套。"""
        fid = str(info.get("FileId") or "")
        if not fid:
            return
        self._meta[fid] = {
            "name": info.get("FileName") or self._meta.get(fid, {}).get("name", ""),
            "size": int(info.get("Size") or 0),
            "etag": info.get("Etag") or "",
            "s3keyFlag": info.get("S3KeyFlag") or "",
        }

    def read_range(self, item: RemoteItem, offset: int, length: int, on_progress=None) -> bytes:
        """回读目标端字节，供迁移后的抽样校验用。

        直链是时效性的，校验时刚传完，缓存里可能没有它 —— 缺元信息就先去列一次
        父目录补上，再换链。
        """
        if str(item.id) not in self._meta:
            try:
                self._children.clear()
                # 用 path 的父目录刷新一次列表，把 _meta 补全
                parent = (item.path or "/").rsplit("/", 1)[0] or "/"
                self.list_dir(parent)
            except Exception:
                pass
        url = self.download_url(item.id, force=True)
        _, headers, body = httpc.request(
            "GET",
            url,
            headers={
                "User-Agent": UA,
                "Referer": f"{API}/",
                "Range": f"bytes={offset}-{offset + length - 1}",
                "Accept-Encoding": "identity",
            },
            timeout=120,
            retries=2,
        )
        if on_progress and body:
            on_progress(len(body))
        return body

    def get_file(self, parent_id: str, name: str) -> RemoteItem | None:
        return self.child(parent_id, name)
