"""夸克网盘适配器（网页 Cookie 登录态）。

实测（2026-10-24，真机验证）：
  夸克网盘走 UC 系 cookie 鉴权，核心 cookie 是 `.quark.cn` 域的 `__pus`/`__puus`/`__uid`
  加 `isg`/`tfstk` 等签名 token。没有扫码/零填写路子，只能把浏览器登录后的 Cookie 粘进来。

地址规律（按 fid 寻址，根目录 fid = "0"）：
  列表   GET  https://drive-pc.quark.cn/1/clouddrive/file/sort?pdir_fid=<id>&...
  下载   POST https://drive-pc.quark.cn/1/clouddrive/file/download   {fids:[...]}
  建目录 POST https://drive-pc.quark.cn/1/clouddrive/file           {pdir_fid, file_name, dir_path, dir_init_lock}
  删除   POST https://drive-pc.quark.cn/1/clouddrive/file/delete    {action_type:1, filelist:[...], exclude_fids:[]}

上传（注意域名是 drive.quark.cn，不是 drive-pc）走 6 步：
  1. POST /file/upload/pre       -> {task_id, upload_id, obj_key, bucket, upload_url, auth_info, callback}
  2. POST /file/update/hash      -> {md5, sha1, task_id}   （秒传校验，md5/sha1 必须是真实的）
  3. 每分片 POST /file/upload/auth 换 OSS 签名（auth_meta 是 OSS StringToSign）
  4. PUT 分片到 https://{bucket}.{upload_url[7:]}/{obj_key}?partNumber=N&uploadId=...
  5. POST /file/upload/auth 换合并签名 -> POST XML 合并
  6. POST /file/upload/finish    -> {obj_key, task_id}

关键坑（务必保留）：
  - 分片固定 4MB（metadata.part_size）。
  - update/hash 的 md5/sha1 必须是真实值，占位全 0 会导致合并 callback 失败、finish 报 43001。
  - PUT 分片 URL 必须带 ?partNumber=N&uploadId=...，漏了会 SignatureDoesNotMatch。
  - auth_meta 里 bucket 用预上传返回的逻辑 bucket（如 ul-sz），服务端会自己换成物理 bucket。
  - 由于 md5/sha1 需要整个文件内容，而引擎是分片流式的，这里用「条件变量按 part_number
    顺序串行累积 hash」——引擎并发派分片，adapter 内部按序号排队处理。
"""
from __future__ import annotations

import base64
import hashlib
import json
import mimetypes
import threading
import time
import urllib.parse
from email.utils import formatdate
from typing import Iterator

from . import httpc
from .base import AdapterError, AuthError, RemoteItem, SinkAdapter, SourceAdapter, UploadSession, account_label
from .httpc import HttpError

WEB = "https://pan.quark.cn"
API_PC = "https://drive-pc.quark.cn/1/clouddrive"
API_UPLOAD = "https://drive.quark.cn/1/clouddrive"
ROOT_ID = "0"
PART_SIZE = 4 * 1024 * 1024  # 夸克固定 4MB 分片
OSS_UA = "aliyun-sdk-js/6.6.1 Chrome 98.0.4758.80 on Windows 10 64-bit"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)


class QuarkError(AdapterError):
    pass


class QuarkAuthError(QuarkError, AuthError):
    """登录态问题：重试无意义，应提示用户重新登录。"""
    retryable = False


def _oss_time() -> str:
    """RFC1123 时间（精确复刻 Go http.TimeFormat）。"""
    return formatdate(timeval=time.time(), localtime=False, usegmt=True)


def _mime(name: str) -> str:
    t, _ = mimetypes.guess_type(name)
    return t or "application/octet-stream"


class _QuarkCommon:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.cookie = (self.cfg.get("cookie") or "").strip()
        self.on_token = None
        self._children: dict[str, list[RemoteItem]] = {}
        self._path_ids: dict[str, str] = {"": ROOT_ID, "/": ROOT_ID}

    def fetch_account(self) -> str:
        """夸克：account/info 直接给昵称。"""
        j = httpc.get_json(
            "https://pan.quark.cn/account/info?fr=pc&platform=pc",
            headers=self._headers(), retries=1,
        )
        # 昵称在 data.nickname 里，不在顶层
        return str((j.get("data") or {}).get("nickname") or j.get("nickname") or "").strip()

    def describe(self) -> dict:
        return {
            "provider": "quark",
            "mode": "cookie",
            "authorized": bool(self.cookie),
            "account": account_label(self, self.fetch_account),
            "scope_note": "网页 Cookie 模式：可读写整个个人网盘（非公开接口）",
        }

    def _headers(self) -> dict:
        return {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Accept": "application/json, text/plain, */*",
            "Content-Type": "application/json",
            "Origin": "https://pan.quark.cn",
            "Referer": "https://pan.quark.cn/",
        }

    def ensure_auth(self) -> None:
        if not self.cookie:
            raise QuarkAuthError(
                "还没登录夸克网盘：请在浏览器打开 pan.quark.cn 登录，"
                "然后按 F12 → Network 里任一 api 请求，把 Cookie 整条复制到「凭据」里"
            )

    def _api(self, path: str, body: dict, base: str = API_PC) -> dict:
        """POST 到夸克接口，返回解析后的 JSON。"""
        self.ensure_auth()
        try:
            _, _, raw = httpc.request(
                "POST",
                f"{base}{path}?pr=ucpro&fr=pc&uc_param_str=",
                headers=self._headers(),
                data=json.dumps(body).encode(),
                timeout=60,
                retries=1,
            )
        except HttpError as exc:
            raise QuarkError(f"夸克接口 {path} 失败（HTTP {exc.status}）：{exc.body[:200]}") from None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise QuarkError(f"夸克接口 {path} 返回非 JSON：{raw.decode('utf-8', 'replace')[:200]}") from None

    def _get_json(self, url: str) -> dict:
        self.ensure_auth()
        try:
            return httpc.get_json(url, headers=self._headers(), retries=1)
        except HttpError as exc:
            raise QuarkError(f"夸克请求失败（HTTP {exc.status}）：{exc.body[:200]}") from None

    # ---------- 目录 ----------
    def list_folder(self, folder_id: str, force: bool = False) -> list[RemoteItem]:
        folder_id = str(folder_id or ROOT_ID)
        if not force and folder_id in self._children:
            return self._children[folder_id]
        items: list[RemoteItem] = []
        page = 1
        while True:
            url = (
                f"{API_PC}/file/sort?pr=ucpro&fr=pc&uc_param_str=&pdir_fid={folder_id}"
                f"&_page={page}&_size=100&_fetch_total=1&_fetch_sub_dirs=0"
                f"&_sort=file_type:asc,updated_at:desc"
            )
            resp = self._get_json(url)
            data = resp.get("data") or {}
            batch = data.get("list") or []
            for raw in batch:
                is_dir = int(raw.get("file_type") or 0) == 0
                items.append(
                    RemoteItem(
                        id=str(raw.get("fid") or ""),
                        name=raw.get("file_name") or "",
                        is_dir=is_dir,
                        size=int(raw.get("size") or 0),
                        md5=(raw.get("md5") or "").upper(),
                        modified=str(raw.get("l_shot_at") or raw.get("updated_at") or ""),
                    )
                )
            total = int((resp.get("metadata") or {}).get("_total") or len(batch) or 0)
            if not batch or len(items) >= total:
                break
            page += 1
            if page > 500:
                break
        self._children[folder_id] = items
        return items

    def resolve_path(self, path: str) -> str:
        """路径 → fid（逐级按名字找，找不到就报错；不自动建目录）。"""
        path = "/" + (path or "/").strip().strip("/")
        if path == "/":
            return ROOT_ID
        if path in self._path_ids:
            return self._path_ids[path]
        current = ROOT_ID
        current_path = ""
        for name in [p for p in path.split("/") if p]:
            current_path += "/" + name
            if current_path in self._path_ids:
                current = self._path_ids[current_path]
                continue
            found = None
            for entry in self.list_folder(current):
                if entry.name == name and entry.is_dir:
                    found = entry
                    break
            if not found:
                raise QuarkError(f"目录不存在：{current_path}")
            current = found.id
            self._path_ids[current_path] = current
        return current

    def list_dir(self, path: str) -> list[RemoteItem]:
        path = "/" + (path or "/").strip().strip("/")
        folder_id = self.resolve_path(path)
        items = self.list_folder(folder_id)
        for entry in items:
            entry.path = (path.rstrip("/") + "/" + entry.name) if path != "/" else "/" + entry.name
        return items

    def _download_url(self, file_id: str) -> str:
        resp = self._api("/file/download", {"fids": [file_id]})
        data = resp.get("data") or []
        if not data:
            raise QuarkError("夸克没有返回下载直链")
        return data[0].get("download_url") or ""


class QuarkSource(_QuarkCommon, SourceAdapter):
    kind = "source"
    label = "夸克网盘"

    def walk_files(self, path: str, on_progress=None) -> Iterator[RemoteItem]:
        yield from self._walk("/" + (path or "/").strip("/"), "", on_progress)

    # 读取走基类通用实现（分段 + 续读 + 进度回报）
    read_max_bytes = 4 * 1024 * 1024
    MAX_CONCURRENT = 4

    def _prepare_download(self, item: RemoteItem, force_refresh: bool = False) -> tuple[str, dict]:
        return self._download_url(item.id), {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Referer": "https://pan.quark.cn/",
        }


class QuarkSink(_QuarkCommon, SinkAdapter):
    """夸克作为目标端。上传见文件头注释的 6 步协议。"""

    kind = "sink"
    label = "夸克网盘"
    supports_empty_file = False
    max_name_bytes = 255

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._sessions: dict[str, dict] = {}
        self._cond = threading.Condition()

    def walk_files(self, path: str) -> dict:
        out: dict[str, tuple[int, RemoteItem]] = {}
        start = "/" + (path or "/").strip().strip("/")
        stack = [(start, "")]
        while stack:
            current, rel = stack.pop()
            try:
                entries = self.list_dir(current)
            except QuarkError:
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
        current_path = ""
        for name in [p for p in path.split("/") if p]:
            current_path += "/" + name
            if current_path in self._path_ids:
                current = self._path_ids[current_path]
                continue
            found = None
            for entry in self.list_folder(current):
                if entry.name == name and entry.is_dir:
                    found = entry
                    break
            if found:
                current = found.id
            else:
                resp = self._api("/file", {
                    "pdir_fid": current, "file_name": name,
                    "dir_path": "", "dir_init_lock": False,
                })
                if resp.get("code") != 0:
                    raise QuarkError(f"夸克建目录失败：{resp.get('message') or resp}")
                current = str((resp.get("data") or {}).get("fid") or "")
                if not current:
                    raise QuarkError(f"夸克建目录没有返回 fid：{str(resp)[:200]}")
            self._path_ids[current_path] = current
            self._children.pop(current, None)
        return current

    def child(self, parent_id: str, name: str) -> RemoteItem | None:
        for entry in self.list_folder(parent_id):
            if entry.name == name:
                return entry
        return None

    def _free_name(self, parent_id: str, name: str) -> str:
        if "." in name:
            stem, ext = name.rsplit(".", 1)
            suffix = "." + ext
        else:
            stem, suffix = name, ""
        n = 1
        while True:
            candidate = f"{stem} ({n}){suffix}"
            if self.child(parent_id, candidate) is None:
                return candidate
            n += 1

    # ---------- 上传 ----------
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
        if size <= 0:
            raise QuarkError("夸克网盘不支持 0 字节文件，这个文件被跳过")
        final_name = name
        existing = self.child(parent_id, name)
        if existing is not None:
            if name_mode == "refuse":
                raise QuarkError(f"目标端已存在同名文件：{name}")
            if name_mode == "auto_rename":
                final_name = self._free_name(parent_id, name)

        now_ms = int(time.time() * 1000)
        resp = self._api("/file/upload/pre", {
            "ccp_hash_update": True,
            "dir_name": "",
            "file_name": final_name,
            "format_type": _mime(final_name),
            "l_created_at": now_ms,
            "l_updated_at": now_ms,
            "pdir_fid": parent_id,
            "size": size,
        }, base=API_UPLOAD)
        if resp.get("code") != 0:
            raise QuarkError(f"夸克预上传失败：{resp.get('message') or resp}")
        pd = resp.get("data") or {}
        task_id = pd.get("task_id") or ""
        if not task_id:
            raise QuarkError(f"夸克预上传没有返回 task_id：{str(resp)[:200]}")

        count = max(1, (size + PART_SIZE - 1) // PART_SIZE)
        self._sessions[task_id] = {
            "pre": pd,
            "parent_id": parent_id,
            "name": final_name,
            "size": size,
            "mime": _mime(final_name),
            "etags": {},
            "md5": hashlib.md5(),
            "sha1": hashlib.sha1(),
            "next_part": 1,
            "total": count,
        }
        parts = [
            {"part_number": seq + 1, "upload_url": f"quark://{task_id}"}
            for seq in range(count)
        ]
        return UploadSession(
            file_id=pd.get("fid") or "",
            upload_id=task_id,
            parts=parts,
            part_size=PART_SIZE,
            size=size,
            name=final_name,
        )

    def _oss_auth(self, info: dict, auth_meta: str) -> str:
        """换 OSS 签名（服务端代签）。"""
        resp = self._api("/file/upload/auth", {
            "auth_info": info["pre"]["auth_info"],
            "auth_meta": auth_meta,
            "task_id": info["pre"]["task_id"],
        }, base=API_UPLOAD)
        if resp.get("code") != 0:
            raise QuarkError(f"夸克换 OSS 签名失败：{resp.get('message') or resp}")
        return (resp.get("data") or {}).get("auth_key") or ""

    def _put_oss(self, info: dict, auth_key: str, data: bytes, headers: dict) -> str:
        pd = info["pre"]
        url = f"https://{pd['bucket']}.{pd['upload_url'][7:]}/{pd['obj_key']}"
        h = {**headers, "Authorization": auth_key, "Referer": "https://pan.quark.cn/"}
        st, resp_headers, _ = httpc.request("PUT", url, headers=h, data=data, timeout=180, retries=2)
        return resp_headers.get("ETag", "")

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        task_id = upload_url.split("://", 1)[-1]
        info = self._sessions.get(task_id)
        if not info:
            raise QuarkError("夸克上传会话已丢失，请重新开始这个文件")
        # 夸克要求 md5/sha1 是真实的整个文件哈希，且分片要按顺序累积。
        # 引擎是并发派分片的，这里用条件变量按 part_number 排队，串行上传 + 顺序累积哈希。
        with self._cond:
            while part_number != info["next_part"]:
                self._cond.wait()
            try:
                pd = info["pre"]
                mime = info["mime"]
                ts = _oss_time()
                auth_meta = (
                    f"PUT\n\n{mime}\n{ts}\nx-oss-date:{ts}\nx-oss-user-agent:{OSS_UA}\n"
                    f"/{pd['bucket']}/{pd['obj_key']}?partNumber={part_number}&uploadId={pd['upload_id']}"
                )
                auth_key = self._oss_auth(info, auth_meta)
                url = f"https://{pd['bucket']}.{pd['upload_url'][7:]}/{pd['obj_key']}?partNumber={part_number}&uploadId={pd['upload_id']}"
                h = {
                    "Content-Type": mime, "x-oss-date": ts, "x-oss-user-agent": OSS_UA,
                    "Authorization": auth_key, "Referer": "https://pan.quark.cn/",
                }
                _, resp_headers, _ = httpc.request("PUT", url, headers=h, data=data, timeout=180, retries=2)
                etag = resp_headers.get("ETag", "")
                # 顺序累积哈希（此时保证是按 part_number 顺序的）
                info["md5"].update(data)
                info["sha1"].update(data)
                info["etags"][part_number] = etag
                return etag
            finally:
                info["next_part"] += 1
                self._cond.notify_all()

    def _commit_oss(self, info: dict, etags: dict) -> None:
        """合并分片（POST XML 到 OSS）。"""
        pd = info["pre"]
        xml_parts = ['<?xml version="1.0" encoding="UTF-8"?>', '<CompleteMultipartUpload>']
        for seq in sorted(etags):
            xml_parts += ['<Part>', f'<PartNumber>{seq}</PartNumber>', f'<ETag>{etags[seq]}</ETag>', '</Part>']
        xml_parts.append('</CompleteMultipartUpload>')
        xml_body = '\n'.join(xml_parts)
        content_md5 = base64.b64encode(hashlib.md5(xml_body.encode()).digest()).decode()
        callback_b64 = base64.b64encode(json.dumps(pd["callback"]).encode()).decode()
        ts = _oss_time()
        commit_meta = (
            f"POST\n{content_md5}\napplication/xml\n{ts}\nx-oss-callback:{callback_b64}\n"
            f"x-oss-date:{ts}\nx-oss-user-agent:{OSS_UA}\n"
            f"/{pd['bucket']}/{pd['obj_key']}?uploadId={pd['upload_id']}"
        )
        auth_key = self._oss_auth(info, commit_meta)
        url = f"https://{pd['bucket']}.{pd['upload_url'][7:]}/{pd['obj_key']}?uploadId={pd['upload_id']}"
        h = {
            "Content-MD5": content_md5, "Content-Type": "application/xml",
            "x-oss-callback": callback_b64, "x-oss-date": ts, "x-oss-user-agent": OSS_UA,
            "Authorization": auth_key, "Referer": "https://pan.quark.cn/",
        }
        st, _, raw = httpc.request("POST", url, headers=h, data=xml_body.encode(), timeout=180, retries=2)
        if st != 200:
            raise QuarkError(f"夸克合并分片失败（HTTP {st}）：{raw.decode('utf-8', 'replace')[:200]}")

    def complete(self, session: UploadSession) -> str:
        task_id = session.upload_id
        info = self._sessions.get(task_id)
        if not info:
            raise QuarkError("夸克上传会话已丢失，请重新开始这个文件")
        total = info["total"]
        if len(info["etags"]) != total:
            raise QuarkError(f"还有 {total - len(info['etags'])} 个分片没传完，不能合并")
        pd = info["pre"]
        # 秒传校验：md5/sha1 是真实哈希（put_part 里顺序累积的）
        md5_hex = info["md5"].hexdigest()
        sha1_hex = info["sha1"].hexdigest()
        resp = self._api("/file/update/hash", {
            "md5": md5_hex, "sha1": sha1_hex, "task_id": task_id,
        }, base=API_UPLOAD)
        if resp.get("code") != 0:
            raise QuarkError(f"夸克哈希校验失败：{resp.get('message') or resp}")
        rapid = bool((resp.get("data") or {}).get("finish"))
        if not rapid:
            self._commit_oss(info, info["etags"])
        fin = self._api("/file/upload/finish", {
            "obj_key": pd["obj_key"], "task_id": task_id,
        }, base=API_UPLOAD)
        if fin.get("code") != 0:
            raise QuarkError(f"夸克完成上传失败：{fin.get('message') or fin}")
        self._sessions.pop(task_id, None)
        self._children.clear()
        return str((fin.get("data") or {}).get("fid") or pd.get("fid") or "")

    def get_file(self, parent_id: str, name: str) -> RemoteItem | None:
        return self.child(parent_id, name)
