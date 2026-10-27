"""天翼云盘适配器（网页登录态）。

实测（2026-10-24，未登录探测）：`cloud.189.cn/api/**` 全部要求 cookie 里带登录会话，
否则统一返回 `{"errorCode":"InvalidSessionKey","errorMsg":"cookieUserSession is null"}`。
所以天翼这边没有「零填写」的路子，认证方式只有一种：把浏览器里登录后的 Cookie 粘进来。

地址规律（和百度/阿里都不一样，这里按 folderId 寻址，不按路径）：
  根目录 folderId = -11
  列目录 /api/open/file/listFiles.action?folderId=<id>&pageNum=1&pageSize=60...
  目录信息 /api/portal/getFolderInfo.action?folderId=<id>
  文件信息 /api/portal/getFileInfo.action?fileId=<id>

工具内部统一用路径，所以在适配器里做一层「路径 → folderId」解析并缓存。
"""
from __future__ import annotations

import base64
import hashlib
import re
import time
import urllib.parse
from typing import Iterator

from . import httpc
from .base import AdapterError, AuthError, RemoteItem, SinkAdapter, SourceAdapter, UploadSession, account_label
from .httpc import HttpError
from ..crypto import (
    aes_ecb_encrypt,
    hmac_sha1_hex,
    random_hex,
    random_uuid4,
    rsa_encrypt_pkcs1,
)

WEB = "https://cloud.189.cn"
UPLOAD_WEB = "https://upload.cloud.189.cn"
ROOT_ID = "-11"
LIST_PATH = "/api/open/file/listFiles.action"
FOLDER_INFO_PATH = "/api/portal/getFolderInfo.action"
FILE_INFO_PATH = "/api/portal/getFileInfo.action"
DOWNLOAD_PATH = "/api/portal/getFileDownloadUrl.action"
# 上传相关
BRIEF_PATH = "/api/portal/v2/getUserBriefInfo.action"
RSA_KEY_PATH = "/api/security/generateRsaKey.action"
CREATE_FOLDER_PATH = "/api/open/file/createFolder.action"
DELETE_PATH = "/api/open/file/deleteFile.action"
# 天翼分片大小固定 10MB，其他值会被服务端拒绝（InvalidPartSize）
SLICE_SIZE = 10 * 1024 * 1024
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)


class TianyiError(AdapterError):
    pass


class TianyiAuthError(TianyiError, AuthError):
    """登录态问题：重试无意义，应提示用户重新登录。"""
    retryable = False


class _TianyiCommon:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.cookie = (self.cfg.get("cookie") or "").strip()
        self.on_token = None
        self._children: dict[str, list[RemoteItem]] = {}
        self._path_ids: dict[str, str] = {"": ROOT_ID, "/": ROOT_ID}
        self._sk_cache: str = ""
        self._rsa_cache: tuple[str, str] | None = None
        self._sessions: dict[str, dict] = {}

    # ---------- 基础 ----------
    def fetch_account(self) -> str:
        """天翼：getUserSizeInfo 返回 account（脱敏手机号/邮箱）。"""
        j = httpc.get_json(
            "https://cloud.189.cn/api/portal/getUserSizeInfo.action",
            headers=self._headers(), retries=1,
        )
        return str(j.get("account") or "").strip()

    def describe(self) -> dict:
        return {
            "provider": "tianyi",
            "mode": "cookie",
            "authorized": bool(self.cookie),
            "account": account_label(self, self.fetch_account),
            "scope_note": "网页 Cookie 模式：可读写整个个人云（非公开接口）",
        }

    def _headers(self) -> dict:
        return {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Referer": "https://cloud.189.cn/web/main/",
            "Accept": "application/json;charset=UTF-8",
        }

    def ensure_auth(self) -> None:
        if not self.cookie:
            raise TianyiAuthError(
                "还没登录天翼云盘：请在浏览器打开 cloud.189.cn 登录，"
                "然后按 F12 → Network 里任一 api 请求，把 Cookie 整条复制到「凭据」里"
            )

    def _get(self, path: str, params: dict | None = None) -> dict:
        self.ensure_auth()
        url = f"{WEB}{path}"
        if params:
            url += "?" + urllib.parse.urlencode(params)
        try:
            return httpc.get_json(url, headers=self._headers(), retries=1)
        except HttpError as exc:
            body = exc.body or ""
            if "InvalidSessionKey" in body:
                raise TianyiAuthError("天翼登录态已失效：请重新复制浏览器里的 Cookie") from None
            raise TianyiError(f"请求 {path} 失败（HTTP {exc.status}）：{body[:200]}") from None

    # ---------- 目录 ----------
    def list_folder(self, folder_id: str, force: bool = False) -> list[RemoteItem]:
        folder_id = str(folder_id or ROOT_ID)
        if not force and folder_id in self._children:
            return self._children[folder_id]
        items: list[RemoteItem] = []
        page = 1
        while True:
            resp = self._get(
                LIST_PATH,
                {
                    "folderId": folder_id,
                    "pageNum": page,
                    "pageSize": 200,
                    "mediaType": 0,
                    "iconOption": 1,
                    "orderBy": "lastOpTime",
                    "descending": "true",
                },
            )
            payload = resp.get("fileListAO") or resp.get("data") or {}
            batch = payload.get("fileList") or payload.get("file") or []
            folders = payload.get("folderList") or []
            # 目录在 folderList（无 size/md5），文件在 fileList（有 size/md5），分开判型。
            for raw in folders:
                items.append(
                    RemoteItem(
                        id=str(raw.get("id") or ""),
                        name=raw.get("name") or "",
                        is_dir=True,
                        size=0,
                        md5="",
                        modified=str(raw.get("lastOpTime") or raw.get("createDate") or ""),
                    )
                )
            for raw in batch:
                items.append(
                    RemoteItem(
                        id=str(raw.get("id") or raw.get("fileId") or ""),
                        name=raw.get("name") or "",
                        is_dir=False,
                        size=int(raw.get("size") or 0),
                        md5=(raw.get("md5") or "").upper(),
                        modified=str(raw.get("lastOpTime") or raw.get("createDate") or ""),
                    )
                )
            total = int(payload.get("count") or 0)
            if len(items) >= total or not batch:
                break
            page += 1
            if page > 200:
                break
        self._children[folder_id] = items
        return items

    def resolve_path(self, path: str) -> str:
        """路径 → folderId（逐级按名字找，找不到就报错；不自动建目录）。"""
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
                raise TianyiError(f"目录不存在：{current_path}")
            current = found.id
            self._path_ids[current_path] = current
        return current

    def list_dir(self, path: str) -> list[RemoteItem]:
        """列某个路径下的条目（Source/Sink 共用），并补上 path 字段。"""
        path = "/" + (path or "/").strip().strip("/")
        folder_id = self.resolve_path(path)
        items = self.list_folder(folder_id)
        for entry in items:
            entry.path = (path.rstrip("/") + "/" + entry.name) if path != "/" else "/" + entry.name
        return items

    def _dlink(self, file_id: str) -> str:
        resp = self._get(DOWNLOAD_PATH, {"fileId": file_id})
        url = resp.get("fileDownloadUrl") or resp.get("downloadUrl") or ""
        if not url:
            info = self._get(FILE_INFO_PATH, {"fileId": file_id})
            url = (
                info.get("fileDownloadUrl")
                or (info.get("file") or {}).get("fileDownloadUrl")
                or ""
            )
        if not url:
            raise TianyiError("天翼没有返回下载直链（接口可能已变更）")
        return url

    # ---------- 上传签名（写入端专用） ----------
    def _session_key(self) -> str:
        """取 sessionKey（顶层字段，不在 data 里）。有缓存，失效时重新拿。"""
        if self._sk_cache:
            return self._sk_cache
        resp = self._get(BRIEF_PATH, {"noCache": f"0.{int(time.time() * 1000)}"})
        sk = resp.get("sessionKey") or ""
        if not sk:
            raise TianyiError(f"取天翼 sessionKey 失败：{str(resp)[:200]}")
        self._sk_cache = sk
        return sk

    def _rsa_key(self) -> tuple[str, str]:
        """取 RSA 公钥，返回 (pubKey, pkId)。有缓存。响应可能是 JSON 或 XML。"""
        if self._rsa_cache:
            return self._rsa_cache
        raw = httpc.get_bytes(
            f"{WEB}{RSA_KEY_PATH}?noCache=0.{int(time.time() * 1000)}",
            headers=self._headers(),
            retries=1,
        ).decode("utf-8", "replace")
        # JSON 形态：{"pubKey":"...","pkId":"..."}
        m_pub = re.search(r'"pubKey"\s*:\s*"([^"]+)"', raw)
        m_pkid = re.search(r'"pkId"\s*:\s*"([^"]+)"', raw)
        if m_pub and m_pkid:
            self._rsa_cache = (m_pub.group(1), m_pkid.group(1))
            return self._rsa_cache
        # XML 形态：<pubKey>...</pubKey><pkId>...</pkId>
        m_pub = re.search(r"<pubKey>([^<]+)</pubKey>", raw)
        m_pkid = re.search(r"<pkId>([^<]+)</pkId>", raw)
        if m_pub and m_pkid:
            self._rsa_cache = (m_pub.group(1), m_pkid.group(1))
            return self._rsa_cache
        raise TianyiError(f"解析天翼 RSA 公钥失败：{raw[:200]}")

    def _signed_call(self, uri: str, payload: dict) -> dict:
        """按天翼上传协议调用 upload.cloud.189.cn 的接口。

        参数用 AES-128-ECB 加密（hex），HMAC-SHA1 签名，会话密钥 RSA 加密放头。
        """
        sk = self._session_key()
        pub, pkid = self._rsa_key()
        plain = "&".join(f"{k}={v}" for k, v in payload.items())
        c = random_hex(16)
        params = aes_ecb_encrypt(c.encode(), plain.encode()).hex()
        ts = str(int(time.time() * 1000))
        sign_src = (
            f"SessionKey={sk}&Operate=GET&RequestURI={uri}&Date={ts}&params={params}"
        )
        headers = {
            **self._headers(),
            "Origin": "https://cloud.189.cn",
            "Referer": "https://cloud.189.cn/",
            "SessionKey": sk,
            "Signature": hmac_sha1_hex(sign_src, c),
            "X-Request-Date": ts,
            "X-Request-ID": random_uuid4(),
            "EncryptionText": rsa_encrypt_pkcs1(pub, c.encode()),
            "PkId": pkid,
        }
        try:
            _, _, raw = httpc.request(
                "GET",
                f"{UPLOAD_WEB}{uri}?params={params}",
                headers=headers,
                timeout=60,
                retries=1,
            )
        except HttpError as exc:
            raise TianyiError(
                f"天翼上传接口 {uri} 失败（HTTP {exc.status}）：{exc.body[:200]}"
            ) from None
        import json

        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except Exception:
            raise TianyiError(f"天翼上传接口 {uri} 返回非 JSON：{raw.decode('utf-8', 'replace')[:200]}") from None

    def _post_open(self, path: str, fields: dict) -> str:
        """POST 到 open 接口（返回 XML），用于建目录/删除。返回原始文本。"""
        body = urllib.parse.urlencode(fields).encode()
        headers = {
            **self._headers(),
            "Content-Type": "application/x-www-form-urlencoded",
        }
        try:
            _, _, raw = httpc.request(
                "POST", f"{WEB}{path}", headers=headers, data=body, timeout=30, retries=0
            )
        except HttpError as exc:
            raise TianyiError(
                f"天翼接口 {path} 失败（HTTP {exc.status}）：{exc.body[:200]}"
            ) from None
        return raw.decode("utf-8", "replace")

    def _create_folder(self, parent_id: str, name: str) -> str:
        """建目录，返回新目录 id。响应可能是 JSON 或 XML，两者都兼容。"""
        text = self._post_open(CREATE_FOLDER_PATH, {"parentFolderId": parent_id, "folderName": name})
        # JSON 形态：{"res_code":0,"id":"..."}
        m = re.search(r'"id"\s*:\s*"?(\d+)"?', text)
        if m:
            return m.group(1)
        # XML 形态：<folder><id>...</id></folder>
        m = re.search(r"<folder>\s*<id>([^<]+)</id>", text)
        if m:
            return m.group(1)
        raise TianyiError(f"天翼建目录失败：{text[:200]}")


class TianyiSource(_TianyiCommon, SourceAdapter):
    kind = "source"
    label = "天翼云盘"

    def walk_files(self, path: str, on_progress=None) -> Iterator[RemoteItem]:
        yield from self._walk("/" + (path or "/").strip("/"), "", on_progress)

    # 读取走基类通用实现（分段 + 续读 + 进度回报）
    read_max_bytes = 4 * 1024 * 1024
    MAX_CONCURRENT = 4

    def _prepare_download(self, item: RemoteItem, force_refresh: bool = False) -> tuple[str, dict]:
        return self._dlink(item.id), {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Referer": "https://cloud.189.cn/web/main/",
        }


class TianyiSink(_TianyiCommon, SinkAdapter):
    """天翼作为目标端。

    上传协议（2026-10-24 真机验证通过）：
      initMultiUpload → getMultiUploadUrls（按分片取直链）→ PUT 分片 → commitMultiUploadFile
    分片大小固定 10MB（服务端只认这个值）。分片 URL 需要「该分片内容的 base64 md5」，
    所以取直链这一步下沉到 put_part 里做（此时才有分片数据可以算 md5）。
    """

    kind = "sink"
    label = "天翼云盘"
    supports_empty_file = False

    def walk_files(self, path: str) -> dict:
        out: dict[str, tuple[int, RemoteItem]] = {}
        start = "/" + (path or "/").strip().strip("/")
        stack = [(start, "")]
        while stack:
            current, rel = stack.pop()
            try:
                entries = self.list_dir(current)
            except TianyiError:
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
        """逐级创建目录，返回最终目录 id（缓存路径映射）。"""
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
                current = self._create_folder(current, name)
            self._path_ids[current_path] = current
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
            raise TianyiError("天翼云盘不支持 0 字节文件，这个文件被跳过")
        final_name = name
        existing = self.child(parent_id, name)
        if existing is not None:
            if name_mode == "refuse":
                raise TianyiError(f"目标端已存在同名文件：{name}")
            if name_mode == "auto_rename":
                final_name = self._free_name(parent_id, name)

        count = max(1, (size + SLICE_SIZE - 1) // SLICE_SIZE)
        payload = {
            "parentFolderId": parent_id,
            "fileName": urllib.parse.quote(final_name),
            "fileSize": size,
            "sliceSize": SLICE_SIZE,
            # 统一 lazyCheck：init 阶段不提供 md5（此时还没读文件内容），
            # md5 在 put_part 里补算、commit 时提交。单/多分片都走这条路。
            "lazyCheck": 1,
        }
        resp = self._signed_call("/person/initMultiUpload", payload)
        if resp.get("code") != "SUCCESS":
            raise TianyiError(f"天翼预上传失败：{str(resp)[:200]}")
        data = resp.get("data") or {}
        upload_file_id = data.get("uploadFileId") or ""
        if not upload_file_id:
            raise TianyiError(f"天翼预上传没有返回 uploadFileId：{str(resp)[:200]}")
        self._sessions[upload_file_id] = {
            "parent_id": parent_id,
            "name": final_name,
            "size": size,
            "part_hex": {},
            "rapid": bool(data.get("fileDataExists")),
        }
        parts = [
            {"part_number": seq + 1, "upload_url": f"tianyi://{upload_file_id}"}
            for seq in range(count)
        ]
        return UploadSession(
            file_id="",
            upload_id=upload_file_id,
            parts=parts,
            part_size=SLICE_SIZE,
            size=size,
            rapid=bool(data.get("fileDataExists")),
            name=final_name,
        )

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        upload_file_id = upload_url.split("://", 1)[-1]
        if upload_file_id not in self._sessions:
            raise TianyiError("天翼上传会话已丢失，请重新开始这个文件")
        part_md5_b64 = base64.b64encode(hashlib.md5(data).digest()).decode()
        part_md5_hex = hashlib.md5(data).hexdigest().upper()
        # 取分片直链（partInfo 用 base64 md5）
        resp = self._signed_call(
            "/person/getMultiUploadUrls",
            {"uploadFileId": upload_file_id, "partInfo": f"{part_number}-{part_md5_b64}"},
        )
        urls = resp.get("uploadUrls") or {}
        part = urls.get(f"partNumber_{part_number}") or {}
        request_url = part.get("requestURL") or ""
        request_header = part.get("requestHeader") or ""
        if not request_url:
            raise TianyiError(f"天翼取分片 {part_number} 直链失败：{str(resp)[:200]}")
        put_headers = {}
        for kv in request_header.split("&"):
            if "=" in kv:
                k, v = kv.split("=", 1)
                put_headers[k] = v
        try:
            _, _, _ = httpc.request(
                "PUT", request_url, headers=put_headers, data=data, timeout=180, retries=2
            )
        except HttpError as exc:
            raise TianyiError(f"天翼上传分片 {part_number} 失败（HTTP {exc.status}）：{exc.body[:200]}") from None
        self._sessions[upload_file_id]["part_hex"][part_number] = part_md5_hex
        return part_md5_hex

    def complete(self, session: UploadSession) -> str:
        upload_file_id = session.upload_id
        info = self._sessions.get(upload_file_id)
        if not info:
            raise TianyiError("天翼上传会话已丢失，请重新开始这个文件")
        count = max(1, (session.size + SLICE_SIZE - 1) // SLICE_SIZE)
        part_hex = info["part_hex"]
        missing = [seq for seq in range(1, count + 1) if seq not in part_hex]
        if missing:
            raise TianyiError(f"还有 {len(missing)} 个分片没传完，不能合并")
        if count == 1:
            single = part_hex[1]
            payload = {
                "uploadFileId": upload_file_id,
                "lazyCheck": 1,
                "fileMd5": single,
                "sliceMd5": single,
            }
        else:
            joined = "\n".join(part_hex[seq] for seq in range(1, count + 1))
            slice_md5 = hashlib.md5(joined.encode()).hexdigest().upper()
            payload = {
                "uploadFileId": upload_file_id,
                "lazyCheck": 1,
                "fileMd5": slice_md5,
                "sliceMd5": slice_md5,
            }
        resp = self._signed_call("/person/commitMultiUploadFile", payload)
        if resp.get("code") != "SUCCESS":
            raise TianyiError(f"天翼合并分片失败：{str(resp)[:200]}")
        self._sessions.pop(upload_file_id, None)
        self._children.clear()
        file = resp.get("file") or {}
        return str(file.get("userFileId") or "")

    def get_file(self, parent_id: str, name: str) -> RemoteItem | None:
        return self.child(parent_id, name)
