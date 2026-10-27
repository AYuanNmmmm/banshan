"""阿里云盘适配器。

同一件事，两套接口地址 —— 这是这个文件里最容易踩的坑：

  官方开放平台（oauth）
      域名  openapi.aliyundrive.com
      路径  /adrive/v1.0/openFile/list | create | complete | get | listUploadedParts
      换令牌 openapi.aliyundrive.com/oauth/access_token（需要 client_id/secret）

  网页登录态（扫码 / web_token）
      域名  api.aliyundrive.com
      路径  /adrive/v3/file/list, /adrive/v2/file/create | complete | get
      换令牌 auth.aliyundrive.com/v2/account/token（只要 refresh_token）

两套凭据互不通用：网页 token 打到 openapi 会 TokenVerifyFailed，反之亦然。
上面这些路径都是实测确认过的，不是照文档猜的。

上传流程两边一致：
  1. create     预上传，提交分片数量，拿回 upload_id + 每片的上传地址
  2. PUT        逐片上传
  3. complete   合并分片
  4. get        回读文件信息，用于完整性校验

差别：网页态没有 listUploadedParts，所以跨进程续传只能依赖本地任务库的记录。
"""
from __future__ import annotations

import time
import urllib.parse

from . import httpc
from .base import AdapterError, AuthError, RemoteItem, SinkAdapter, SourceAdapter, UploadSession, account_label
from .httpc import HttpError

OPEN_HOST = "https://openapi.aliyundrive.com"
WEB_HOST = "https://api.aliyundrive.com"
OAUTH_BASE = "https://openapi.aliyundrive.com/oauth"
WEB_REFRESH_URL = "https://auth.aliyundrive.com/v2/account/token"
ROOT_ID = "root"

WEB_PATHS = {
    "list": "/adrive/v3/file/list",
    "create": "/adrive/v2/file/create",
    "complete": "/adrive/v2/file/complete",
    "get": "/adrive/v2/file/get",
    "uploaded": "",
    "download": "/adrive/v2/file/get_download_url",
    "user": "/adrive/v2/user/get",
}

OPEN_PATHS = {
    "list": "/adrive/v1.0/openFile/list",
    "create": "/adrive/v1.0/openFile/create",
    "complete": "/adrive/v1.0/openFile/complete",
    "get": "/adrive/v1.0/openFile/get",
    "uploaded": "/adrive/v1.0/openFile/listUploadedParts",
    "download": "/adrive/v1.0/openFile/getDownloadUrl",
    "user": "/adrive/v1.0/user/getDriveInfo",
}


class AliyunError(AdapterError):
    pass


class AliyunAuthError(AliyunError, AuthError):
    """登录态问题：重试无意义，应提示用户重新登录。"""
    retryable = False


def _raise(resp: dict, action: str) -> None:
    code = resp.get("code") or resp.get("error")
    if not code:
        return
    msg = resp.get("message") or resp.get("error_description") or ""
    hints = {
        "AccessTokenInvalid": "access_token 无效或已过期，请重新登录",
        "AccessTokenExpired": "access_token 已过期，请重新登录",
        "TokenVerifyFailed": "凭据类型不匹配（网页登录态和开放平台令牌不通用）",
        "ForbiddenFileInTheRecycleBin": "文件在回收站中",
        "NotFound.File": "文件或目录不存在",
        "PreHashMatched": "同名同哈希文件已存在",
        "Conflict.File": "目标目录存在同名文件",
        "QuotaExhausted.Drive": "云盘空间不足",
        "UserNotAllowedAccessDrive": "无权访问该云盘",
        "InvalidParameter.ClientId": "这个接口需要 client_id，网页态应改用 auth 域名换令牌",
    }
    raise AliyunError(f"{action}失败：{hints.get(code, code)} {msg}".strip())


class _AliyunCommon:
    def fetch_account(self) -> str:
        """阿里：user/get 返回 nick_name（没设昵称时是 user_name）。"""
        u = self._post(self.paths["user"], {})
        return str(u.get("nick_name") or u.get("user_name") or "").strip()

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        # 扫码登录拿到的是网页登录态，走 api.aliyundrive.com 那套
        # 注意默认值不能是 "oauth"：凭据归档到 accounts 后没有 auth_mode 字段，
        # 若默认成 oauth，续期会去找 client_id 而失败
        raw_mode = (self.cfg.get("auth_mode") or "").lower()
        # 只有明确声明 oauth 才走官方 OAuth（那才需要 client_id/client_secret）。
        # 扫码登录拿到的 token 属于 web_token，续期也走 exchange_web_token；
        # 早先这里把「auth_mode 为空」也判成 oauth，导致凭据归档到 accounts 后
        # 续期时要求 client_id 而失败（400 client_id 不能为空）。
        self.mode = "oauth" if raw_mode == "oauth" else "web_token"
        self.on_token = None
        self.client_id = (self.cfg.get("client_id") or "").strip()
        self.client_secret = (self.cfg.get("client_secret") or "").strip()
        self.access_token = (self.cfg.get("access_token") or "").strip()
        self.refresh_token = (self.cfg.get("refresh_token") or "").strip()
        self.expires_at = int(self.cfg.get("expires_at") or 0)
        self.drive_id = (self.cfg.get("drive_id") or "").strip()

        if self.mode == "web_token":
            self.host = (self.cfg.get("api_base") or "").strip() or WEB_HOST
            self.paths = dict(WEB_PATHS)
        else:
            self.host = (self.cfg.get("api_base") or "").strip() or OPEN_HOST
            self.paths = dict(OPEN_PATHS)

        self._dir_ids: dict[str, str] = {"": ROOT_ID, "/": ROOT_ID}
        self._children: dict[str, list[RemoteItem]] = {}
        self._download_cache: dict[str, tuple[str, float]] = {}

    # ---------- 授权 ----------
    def authorize_url(self, redirect_uri: str) -> str:
        if not self.client_id:
            raise AliyunError("缺少 client_id，请先在阿里云盘开放平台创建应用")
        query = urllib.parse.urlencode(
            {
                "client_id": self.client_id,
                "response_type": "code",
                "redirect_uri": redirect_uri,
                "scope": "user:base,file:all:read,file:all:write",
            }
        )
        return f"{OAUTH_BASE}/authorize?{query}"

    def _emit_token(self, payload: dict) -> None:
        if self.on_token:
            try:
                self.on_token(payload)
            except Exception:
                pass

    def _token_request(self, payload: dict) -> dict:
        data = httpc.post_json(f"{OAUTH_BASE}/access_token", payload, retries=2)
        if data.get("code") or data.get("error"):
            raise AliyunError(
                f"换取令牌失败：{data.get('message') or data.get('error_description') or data.get('code')}"
            )
        self.access_token = data.get("access_token", "")
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        expires_in = int(data.get("expires_in") or 7200)
        self.expires_at = int(time.time()) + expires_in
        result = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": expires_in,
            "expires_at": self.expires_at,
        }
        self._emit_token(result)
        return result

    def exchange_code(self, code: str, redirect_uri: str) -> dict:
        return self._token_request(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "authorization_code",
                "code": code.strip(),
                "redirect_uri": redirect_uri,
            }
        )

    def exchange_web_token(self, refresh_token: str) -> dict:
        """网页登录态续期。

        实测：只有 auth.aliyundrive.com/v2/account/token 认这个 refresh_token，
        api.aliyundrive.com/token/refresh 会报 InvalidParameter.ClientId。
        返回里带 default_drive_id，正好省掉一次查询。
        """
        url = (self.cfg.get("web_refresh_url") or "").strip() or WEB_REFRESH_URL
        data = httpc.post_json(
            url,
            {"refresh_token": refresh_token.strip(), "grant_type": "refresh_token"},
            headers={
                "Content-Type": "application/json",
                "Referer": "https://www.aliyundrive.com/",
            },
            retries=2,
        )
        if not data.get("access_token"):
            raise AliyunError(
                f"网页登录态续期失败：{data.get('message') or data.get('code') or data}"
            )
        self.access_token = data["access_token"]
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        self.expires_at = int(time.time()) + int(data.get("expires_in") or 7200)
        if data.get("default_drive_id"):
            self.drive_id = data["default_drive_id"]
        result = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": data.get("expires_in"),
            "expires_at": self.expires_at,
        }
        self._emit_token(result)
        return result

    def refresh(self) -> dict:
        if self.mode == "web_token":
            if not self.refresh_token:
                raise AliyunError("缺少 refresh_token，请重新扫码登录")
            return self.exchange_web_token(self.refresh_token)
        if not self.refresh_token:
            raise AliyunError("缺少 refresh_token，请重新授权")
        return self._token_request(
            {
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
            }
        )

    def ensure_token(self) -> None:
        if not self.access_token:
            if self.refresh_token:
                self.refresh()
                if self.access_token:
                    return
            raise AliyunAuthError("未授权：请先扫码登录阿里云盘")
        if self.expires_at and self.expires_at - time.time() < 300 and self.refresh_token:
            self.refresh()

    # ---------- 基础调用 ----------
    def _post(self, path: str, payload: dict, retries: int = 3) -> dict:
        if not path:
            raise AliyunError("当前认证方式不支持这个操作")
        self.ensure_token()
        body = dict(payload)
        if self.drive_id:
            body.setdefault("drive_id", self.drive_id)
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            "Content-Type": "application/json",
            "Referer": "https://www.aliyundrive.com/",
            "Origin": "https://www.aliyundrive.com",
        }
        try:
            return httpc.post_json(f"{self.host}{path}", body, headers=headers, retries=retries)
        except HttpError as exc:
            if exc.status == 401 and self.refresh_token:
                self.refresh()
                headers["Authorization"] = f"Bearer {self.access_token}"
                return httpc.post_json(f"{self.host}{path}", body, headers=headers, retries=1)
            raise AliyunError(
                f"请求 {path} 失败（HTTP {exc.status}）：{exc.body[:200]}"
            ) from None

    def ensure_drive(self) -> str:
        if self.drive_id:
            return self.drive_id
        if not self.access_token and self.refresh_token:
            self.refresh()
        if self.drive_id:
            return self.drive_id
        self.ensure_token()
        info = self._post(self.paths["user"], {})
        self.drive_id = (
            info.get("default_drive_id")
            or info.get("resource_drive_id")
            or info.get("drive_id")
            or ""
        )
        if not self.drive_id:
            raise AliyunError("拿不到 drive_id，请重新登录")
        return self.drive_id

    def list_children(self, parent_id: str, force: bool = False) -> list[RemoteItem]:
        if not force and parent_id in self._children:
            return self._children[parent_id]
        self.ensure_drive()
        items: list[RemoteItem] = []
        marker = ""
        while True:
            payload = {
                "drive_id": self.drive_id,
                "parent_file_id": parent_id,
                "limit": 200,
                "order_by": "name",
                "order_direction": "ASC",
                "fields": "*",
            }
            if marker:
                payload["marker"] = marker
            resp = self._post(self.paths["list"], payload)
            _raise(resp, "列目录")
            for raw in resp.get("items") or []:
                items.append(
                    RemoteItem(
                        id=raw.get("file_id") or raw.get("id") or "",
                        name=raw.get("name") or "",
                        is_dir=(raw.get("type") == "folder"),
                        size=int(raw.get("size") or 0),
                        sha1=(raw.get("content_hash") or "")
                        if (raw.get("content_hash_name") or "").lower() == "sha1"
                        else "",
                        modified=str(raw.get("updated_at") or ""),
                    )
                )
            marker = resp.get("next_marker") or ""
            if not marker:
                break
        self._children[parent_id] = items
        return items

    def resolve_path_readonly(self, path: str) -> str:
        """只查不建，路径不存在时抛错。"""
        if not path or path == "/":
            return ROOT_ID
        parts = [p for p in path.split("/") if p]
        current = ROOT_ID
        for depth, name in enumerate(parts):
            found = None
            for entry in self.list_children(current):
                if entry.name == name and entry.is_dir:
                    found = entry
                    break
            if not found:
                raise AliyunError(f"目录不存在：/{'/'.join(parts[: depth + 1])}")
            current = found.id
        return current

    def resolve_path(self, path: str) -> str:
        """把 /a/b/c 逐级建出来，返回末级 file_id。"""
        if not path or path == "/":
            return ROOT_ID
        if path in self._dir_ids:
            return self._dir_ids[path]
        parts = [p for p in path.split("/") if p]
        current_id = ROOT_ID
        current_path = ""
        for name in parts:
            current_path = current_path + "/" + name
            if current_path in self._dir_ids:
                current_id = self._dir_ids[current_path]
                continue
            existing = None
            for entry in self.list_children(current_id):
                if entry.name == name and entry.is_dir:
                    existing = entry
                    break
            if existing:
                current_id = existing.id
            else:
                resp = self._post(
                    self.paths["create"],
                    {
                        "drive_id": self.drive_id,
                        "parent_file_id": current_id,
                        "name": name,
                        "type": "folder",
                        "check_name_mode": "refuse",
                    },
                )
                _raise(resp, f"创建目录 {name}")
                current_id = resp.get("file_id") or resp.get("drive_id") or ""
                self._children.pop(current_id, None)
            self._dir_ids[current_path] = current_id
        return current_id

    def find_child(self, parent_id: str, name: str) -> RemoteItem | None:
        for entry in self.list_children(parent_id):
            if entry.name == name:
                return entry
        return None

    def _download_url(self, file_id: str, force: bool = False) -> str:
        """取下载直链。

        **必须带 drive_id**。之前这里只传了 file_id + expire_sec，阿里会回
        400「The input parameter share_id, drive_id is not valid. get file
        request drive_id and share_id are both nil」—— 服务端看到两个都没传，
        就既不像个人网盘文件、也不像分享文件，直接拒掉。
        这个坑只在「阿里当源端」时才会踩到（当目标端只走上传，不调这个接口），
        所以之前一直没暴露。
        """
        cached = self._download_cache.get(file_id)
        if cached and not force and cached[1] > time.time():
            return cached[0]
        # drive_id 可能是懒加载的（登录后第一次访问才去查 /user/get），
        # 首次下载必须确保它已经就位，否则又变成「both nil」。
        drive_id = self.ensure_drive()
        resp = self._post(
            self.paths["download"],
            {"drive_id": drive_id, "file_id": file_id, "expire_sec": 14400},
        )
        url = resp.get("url") or ""
        if not url:
            _raise(resp, "获取下载链接")
            raise AliyunError("没拿到下载链接")
        self._download_cache[file_id] = (url, time.time() + 3600)
        return url


class AliyunSink(_AliyunCommon, SinkAdapter):
    kind = "sink"
    label = "阿里云盘"

    # 实测（2026-10-25）：阿里云盘的上传会话是 sequential multipart。
    # 直接 PUT part 2（跳过 part 1）会 400：
    #   <Code>PartNotSequential</Code>
    #   For sequential multipart upload, you must upload or complete parts
    #   with sequential part number.
    # 改成 1→2→3 顺序传就正常。这是服务端硬约束，没有参数能关掉，
    # 所以由引擎把该文件的上传并发压到 1（见 engine._upload_parts）。
    sequential_parts = True

    def describe(self) -> dict:
        return {
            "provider": "aliyun",
            "mode": self.mode,
            "authorized": bool(self.access_token or self.refresh_token),
            "drive_id": self.drive_id,
            "writable": True,
            "account": account_label(self, self.fetch_account),
        }

    def walk_files(self, path: str) -> dict:
        """只读递归列举，用于增量比对（不创建任何东西）。"""
        out: dict[str, tuple[int, RemoteItem]] = {}
        self.ensure_drive()
        try:
            start = self.resolve_path_readonly(path)
        except AliyunError:
            return out
        stack = [(start, "")]
        while stack:
            parent_id, rel = stack.pop()
            for entry in self.list_children(parent_id):
                child_rel = (rel + "/" + entry.name).lstrip("/")
                if entry.is_dir:
                    stack.append((entry.id, child_rel))
                else:
                    out[child_rel] = (entry.size, entry)
        return out

    def ensure_dir(self, path: str) -> str:
        self.ensure_drive()
        return self.resolve_path(path)

    def child(self, parent_id: str, name: str) -> RemoteItem | None:
        return self.find_child(parent_id, name)

    def browse(self, path: str) -> list[RemoteItem]:
        self.ensure_drive()
        parent_id = self.resolve_path_readonly(path)
        items = self.list_children(parent_id)
        for entry in items:
            entry.path = path.rstrip("/") + "/" + entry.name
        return items

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
        self.ensure_drive()
        part_count = max(1, (size + part_size - 1) // part_size)
        mode = name_mode if name_mode in ("refuse", "auto_rename", "ignore") else "refuse"
        payload = {
            "drive_id": self.drive_id,
            "parent_file_id": parent_id,
            "name": name,
            "type": "file",
            "size": size,
            "check_name_mode": mode,
            "content_type": "application/octet-stream",
            "part_info_list": [{"part_number": i + 1} for i in range(part_count)],
        }
        resp = self._post(self.paths["create"], payload)
        _raise(resp, f"预上传 {name}")
        upload_id = resp.get("upload_id") or ""
        parts = resp.get("part_info_list") or []
        # auto_rename 时服务端会回一个新文件名，回读校验得按它去找
        final_name = resp.get("name") or name
        self._children.pop(parent_id, None)
        if not upload_id:
            # 没有 upload_id 但给了 file_id：服务端认为这个文件已经存在（同名同内容，
            # 或旧会话残留的完整文件）。这时候不该报错 —— 直接当「秒传命中」收下，
            # 一个字节都不用传。之前这里直接抛错，会凭空多出一堆「失败」。
            file_id = resp.get("file_id") or ""
            if file_id:
                return UploadSession(
                    file_id=file_id,
                    upload_id="",
                    parts=[],
                    part_size=part_size,
                    size=size,
                    rapid=True,
                    name=final_name,
                )
            raise AliyunError(f"预上传没有返回 upload_id/分片地址：{str(resp)[:200]}")
        if not parts:
            raise AliyunError(f"预上传没有返回 upload_id/分片地址：{str(resp)[:200]}")
        return UploadSession(
            file_id=resp.get("file_id") or "",
            upload_id=upload_id,
            parts=parts,
            part_size=part_size,
            size=size,
            rapid=bool(resp.get("rapid_upload")),
            name=final_name,
        )

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        """上传单个分片。

        注意 Content-Type 必须显式置空：预签名 URL 是按空 Content-Type 签的，
        一旦带上 application/octet-stream 就会 SignatureDoesNotMatch（403）；
        而完全不给这个头，urllib 又会自动补一个 x-www-form-urlencoded，
        同样是 403。所以只能给一个空值。
        """
        _, resp_headers, _ = httpc.request(
            "PUT",
            upload_url,
            headers={"Content-Type": ""},
            data=data,
            timeout=180,
            retries=3,
        )
        for key, value in resp_headers.items():
            if key.lower() == "etag":
                return value.strip('"')
        return ""

    def list_uploaded(self, session: UploadSession) -> list[int]:
        # 网页态没有这个接口；靠本地任务库记录已完成分片
        if not self.paths["uploaded"]:
            return []
        try:
            resp = self._post(
                self.paths["uploaded"],
                {"file_id": session.file_id, "upload_id": session.upload_id},
            )
        except AliyunError:
            return []
        done = []
        for raw in resp.get("uploaded") or []:
            try:
                done.append(int(raw.get("part_number")))
            except (TypeError, ValueError):
                continue
        return done

    def complete(self, session: UploadSession) -> str:
        # 和下载一样：drive_id 必须显式带上，别指望服务端从 token 推断
        # （推断不出来时就是 400「drive_id and share_id are both nil」）。
        resp = self._post(
            self.paths["complete"],
            {
                "drive_id": self.ensure_drive(),
                "file_id": session.file_id,
                "upload_id": session.upload_id,
            },
            retries=5,
        )
        _raise(resp, "合并分片")
        # 目录内容变了，缓存必须失效，否则同目录的下一个文件还会以为
        # 这里没有同名文件，白白撞一次「已存在」
        self._children.clear()
        return resp.get("file_id") or session.file_id

    def get_file(self, parent_id: str, name: str) -> RemoteItem | None:
        return self.find_child(parent_id, name)

    def get_file_by_id(self, file_id: str) -> RemoteItem:
        resp = self._post(
            self.paths["get"], {"drive_id": self.ensure_drive(), "file_id": file_id}
        )
        _raise(resp, "读取文件信息")
        return RemoteItem(
            id=file_id,
            name=resp.get("name") or "",
            is_dir=False,
            size=int(resp.get("size") or 0),
            sha1=(resp.get("content_hash") or "")
            if (resp.get("content_hash_name") or "").lower() == "sha1"
            else "",
        )

    def _prepare_download(self, item: RemoteItem, force_refresh: bool = False) -> tuple[str, dict]:
        """阿里下载直链有时效，force_refresh 时重新取一次。"""
        url = self._download_url(item.id, force=force_refresh) if force_refresh \
            else self._download_url(item.id)
        return url, {"Referer": "https://www.aliyundrive.com/"}

    def quota(self) -> dict | None:
        """容量：网页态走 /v2/drive/get（注意没有 adrive 前缀，加了就 404）。"""
        try:
            drive_id = self.ensure_drive()
        except Exception:
            return None
        try:
            if self.mode == "web_token":
                resp = self._post("/v2/drive/get", {"drive_id": drive_id})
            else:
                resp = self._post(self.paths["user"], {"drive_id": drive_id})
        except Exception:
            return None
        total = int(resp.get("total_size") or resp.get("drive_total_size") or 0)
        used = int(resp.get("used_size") or resp.get("drive_used_size") or 0)
        if total <= 0:
            return None
        return {"total": total, "used": used, "free": max(0, total - used)}


class AliyunSource(_AliyunCommon, SourceAdapter):
    kind = "source"
    label = "阿里云盘"

    def describe(self) -> dict:
        return {
            "provider": "aliyun",
            "mode": self.mode,
            "authorized": bool(self.access_token or self.refresh_token),
            "drive_id": self.drive_id,
            "account": account_label(self, self.fetch_account),
        }

    def list_dir(self, path: str) -> list[RemoteItem]:
        if not path.startswith("/"):
            path = "/" + path
        parent_id = self.resolve_path_readonly(path)
        items = self.list_children(parent_id)
        for entry in items:
            entry.path = path.rstrip("/") + "/" + entry.name
        return items

    # 读取走基类 SourceAdapter 的通用实现（分段 + 续读 + 进度回报），
    # 只需提供取直链的方式即可。
    read_max_bytes = 4 * 1024 * 1024
    MAX_CONCURRENT = 4

    def _prepare_download(self, item: RemoteItem, force_refresh: bool = False) -> tuple[str, dict]:
        url = self._download_url(item.id, force=force_refresh) if force_refresh \
            else self._download_url(item.id)
        return url, {"Referer": "https://www.aliyundrive.com/"}
