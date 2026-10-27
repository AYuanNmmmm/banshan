"""百度网盘适配器。

两种认证方式共存：
  oauth  —— 走 pan.baidu.com/union 开放平台，合规、稳定，但只能访问 /apps/<应用名>/
  cookie —— 走网页端接口，可读全盘，但属非公开接口，可能随时失效，且不符合平台协议

注意：官方约定下载 dlink 时必须带 User-Agent: pan.baidu.com，否则返回 403。
"""
from __future__ import annotations

import hashlib
import http.client
import json
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

from . import httpc
from .base import AdapterError, AuthError, RemoteItem, SinkAdapter, SourceAdapter, UploadSession, account_label
from .httpc import HttpError

OAUTH_BASE = "https://openapi.baidu.com/oauth/2.0"
XPAN_BASE = "https://pan.baidu.com/rest/2.0/xpan"
WEB_BASE = "https://pan.baidu.com"

# 官方 API 的目录硬限制：只能操作 /apps 下属于本应用的目录
APPS_PREFIX = "/apps"

ERRNO_MSG = {
    0: "成功",
    -6: "身份验证失败，access_token 可能已过期",
    2: "参数错误",
    12: "目录不存在",
    111: "access_token 已失效，需要重新授权",
    31066: "文件不存在或无权访问",
    31023: "接口参数不合法",
}


class BaiduError(AdapterError):
    pass


class BaiduAuthError(BaiduError, AuthError):
    """登录态问题：重试无意义，应提示用户重新登录。"""
    retryable = False


def _err(errno: int, extra: str = "") -> BaiduError:
    msg = ERRNO_MSG.get(errno, "未知错误")
    return BaiduError(f"百度网盘返回 errno={errno}（{msg}）{('：' + extra) if extra else ''}")


class _BaiduCommon:
    def fetch_account(self) -> str:
        """百度：xpan 的 uinfo 会返回网盘昵称（netdisk_name）和账号昵称（baidu_name）。"""
        j = httpc.get_json(
            "https://pan.baidu.com/rest/2.0/xpan/nas?method=uinfo",
            headers=self._cookie_headers(), retries=1,
        )
        if j.get("errno") not in (0, None):
            return ""
        return str(j.get("netdisk_name") or j.get("baidu_name") or "").strip()

    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        # 扫码登录拿到的就是网页登录态（BDUSS），所以和 cookie 模式是同一套实现
        raw_mode = (self.cfg.get("auth_mode") or "oauth").lower()
        self.mode = "cookie" if raw_mode in ("cookie", "qr") else "oauth"
        self.on_token = None
        self.client_id = (self.cfg.get("client_id") or "").strip()
        self.client_secret = (self.cfg.get("client_secret") or "").strip()
        self.access_token = (self.cfg.get("access_token") or "").strip()
        self.refresh_token = (self.cfg.get("refresh_token") or "").strip()
        self.cookie = (self.cfg.get("cookie") or "").strip()
        self._dlink_cache: dict[str, tuple[str, float]] = {}
        self._dir_cache: dict[str, list[RemoteItem]] = {}
        # dlink 取直链必须串行：引擎是「多分片并发」，一旦缓存在某一刻过期，
        # N 个分片线程会同时去打 filemetas，等于对同一个文件发起 N 次并发取链，
        # 容易被百度判定为异常请求（后续下载 403 31326）。这里加锁只让一个线程
        # 真正去取，其余等它取完直接复用结果。
        self._dlink_lock = threading.Lock()
        # 下载并发的闸门由基类 SourceAdapter._read_sem 按 MAX_CONCURRENT 统一管，
        # 不在这里重复建信号量（否则连 Sink 实例也会带一个用不上的）。

    # ---------- 授权 ----------
    def authorize_url(self, redirect_uri: str = "oob") -> str:
        if not self.client_id:
            raise BaiduError("缺少 client_id（AppKey），请先在百度网盘开放平台创建应用")
        query = urllib.parse.urlencode(
            {
                "response_type": "code",
                "client_id": self.client_id,
                "redirect_uri": redirect_uri,
                "scope": "basic,netdisk",
                "display": "page",
            }
        )
        return f"{OAUTH_BASE}/authorize?{query}"

    def _token_endpoint(self, params: dict) -> dict:
        url = f"{OAUTH_BASE}/token?" + urllib.parse.urlencode(params)
        data = httpc.get_json(url, retries=2)
        if "error" in data:
            raise BaiduError(f"换取令牌失败：{data.get('error_description') or data['error']}")
        self.access_token = data.get("access_token", "")
        self.refresh_token = data.get("refresh_token", self.refresh_token)
        result = {
            "access_token": self.access_token,
            "refresh_token": self.refresh_token,
            "expires_in": data.get("expires_in", 0),
            "expires_at": int(time.time()) + int(data.get("expires_in") or 0),
        }
        if self.on_token:
            try:
                self.on_token(result)
            except Exception:
                pass
        return result

    def exchange_code(self, code: str, redirect_uri: str = "oob") -> dict:
        return self._token_endpoint(
            {
                "grant_type": "authorization_code",
                "code": code.strip(),
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "redirect_uri": redirect_uri,
            }
        )

    def refresh(self) -> dict:
        if not self.refresh_token:
            raise BaiduError("缺少 refresh_token，请重新授权")
        return self._token_endpoint(
            {
                "grant_type": "refresh_token",
                "refresh_token": self.refresh_token,
                "client_id": self.client_id,
                "client_secret": self.client_secret,
            }
        )

    def ensure_token(self) -> None:
        if self.mode == "cookie":
            if not self.cookie:
                raise BaiduAuthError("cookie 模式下需要填写 BDUSS")
            return
        if not self.access_token:
            raise BaiduAuthError("未授权：请先完成百度网盘授权")
        expires_at = int(self.cfg.get("expires_at") or 0)
        if expires_at and expires_at - time.time() < 86400 and self.refresh_token:
            self.refresh()

    # ---------- 通用 ----------
    def _cookie_headers(self) -> dict:
        return {
            "Cookie": self.cookie,
            "User-Agent": "netdisk;P2SP;3.0.20.135",
            "Referer": "https://pan.baidu.com/disk/home",
        }

    def _api_headers(self) -> dict:
        return {"User-Agent": "pan.baidu.com"}

    def _list_oauth(self, path: str) -> list[RemoteItem]:
        if not path.startswith(APPS_PREFIX):
            raise BaiduError(
                "官方接口只能访问 /apps 下的应用目录（例如 /apps/panmigrate）。"
                "请把要迁移的文件先移入该目录，或改用 cookie 模式。"
            )
        items: list[RemoteItem] = []
        start = 0
        while True:
            query = urllib.parse.urlencode(
                {
                    "method": "list",
                    "access_token": self.access_token,
                    "dir": path,
                    "start": start,
                    "limit": 1000,
                    "order": "name",
                    "desc": 0,
                    "web": 1,
                }
            )
            data = httpc.get_json(f"{XPAN_BASE}/file?{query}", headers=self._api_headers())
            errno = data.get("errno", 0)
            if errno != 0:
                raise _err(errno, path)
            batch = data.get("list") or []
            for raw in batch:
                items.append(
                    RemoteItem(
                        id=str(raw.get("fs_id")),
                        name=raw.get("server_filename", ""),
                        is_dir=bool(raw.get("isdir")),
                        path=raw.get("path", ""),
                        size=int(raw.get("size") or 0),
                        md5=raw.get("md5") or "",
                        modified=str(raw.get("server_mtime") or ""),
                    )
                )
            if len(batch) < 1000:
                break
            start += 1000
        return items

    def _list_cookie(self, path: str) -> list[RemoteItem]:
        items: list[RemoteItem] = []
        page = 1
        while True:
            query = urllib.parse.urlencode(
                {"dir": path, "page": page, "num": 1000, "order": "name", "desc": 0}
            )
            data = httpc.get_json(
                f"{WEB_BASE}/api/list?{query}", headers=self._cookie_headers()
            )
            errno = data.get("errno", 0)
            if errno != 0:
                raise _err(errno, path)
            batch = data.get("list") or []
            for raw in batch:
                items.append(
                    RemoteItem(
                        id=str(raw.get("fs_id")),
                        name=raw.get("server_filename", ""),
                        is_dir=bool(raw.get("isdir")),
                        path=raw.get("path", ""),
                        size=int(raw.get("size") or 0),
                        md5=raw.get("md5") or "",
                        modified=str(raw.get("server_mtime") or ""),
                    )
                )
            if len(batch) < 1000:
                break
            page += 1
        return items

    def list_dir(self, path: str) -> list[RemoteItem]:
        if not path.startswith("/"):
            path = "/" + path
        if path in self._dir_cache:
            return self._dir_cache[path]
        self.ensure_token()
        items = self._list_cookie(path) if self.mode == "cookie" else self._list_oauth(path)
        self._dir_cache[path] = items
        return items

    def _dlink_oauth(self, fs_id: str) -> str:
        query = urllib.parse.urlencode(
            {
                "method": "filemetas",
                "access_token": self.access_token,
                "fsids": f"[{fs_id}]",
                "dlink": 1,
                "thumb": 0,
                "extra": 0,
            }
        )
        data = httpc.get_json(f"{XPAN_BASE}/multimedia?{query}", headers=self._api_headers())
        if data.get("errno", 0) != 0:
            raise _err(data.get("errno", 0), "获取下载链接")
        for raw in data.get("list") or []:
            if raw.get("dlink"):
                return raw["dlink"]
        raise BaiduError("没拿到下载直链，可能是该文件不允许下载或需要会员权限")

    def _dlink_cookie(self, fs_id: str) -> str:
        """网页态取下载直链。

        实测两个坑：
          1. 不能带 target 参数，带了会报 errno=12；
          2. 结果在 info 里，不是 list 里。
        """
        query = urllib.parse.urlencode({"dlink": 1, "fsids": f"[{fs_id}]"})
        data = httpc.get_json(
            f"{WEB_BASE}/api/filemetas?{query}", headers=self._cookie_headers()
        )
        if data.get("errno", 0) != 0:
            raise _err(data.get("errno", 0), "获取下载链接")
        for bucket in ("info", "list"):
            for raw in data.get(bucket) or []:
                if isinstance(raw, dict) and raw.get("dlink"):
                    return raw["dlink"]
        raise BaiduError("没拿到下载直链（网页接口可能已变更）")

    def dlink(self, item: RemoteItem, force: bool = False) -> str:
        """取下载直链。加锁串行，避免多分片并发重复打 filemetas。"""
        # 直链剩余有效期低于这个阈值就提前换新，别让一片下到一半过期。
        # 单分片 4MB，百度非会员限速约 128KB/s 时需要 30 秒以上，
        # 留 90 秒余量保证这一片能平安下完。
        SAFE_MARGIN = 90

        def _cached() -> str:
            cached = self._dlink_cache.get(item.id)
            if cached and cached[1] - time.time() > SAFE_MARGIN:
                return cached[0]
            return ""

        if not force:
            fresh = _cached()
            if fresh:
                return fresh

        with self._dlink_lock:
            # 双检：等锁期间可能已有别的线程取好了，直接用，别重复请求
            if not force:
                fresh = _cached()
                if fresh:
                    return fresh
            self.ensure_token()
            url = (
                self._dlink_cookie(item.id) if self.mode == "cookie" else self._dlink_oauth(item.id)
            )
            if self.access_token and "access_token=" not in url:
                url += ("&" if "?" in url else "?") + f"access_token={self.access_token}"
            # 百度 dlink 的 sign 有时效（实测约 5~10 分钟），且可能与出口 IP 绑定。
            # 这里按 5 分钟缓存 + 90 秒安全余量，等于每 3.5 分钟自动换新链接；
            # 被拒时再由 invalidate() 立即清掉，避免拿着坏链接反复撞墙。
            self._dlink_cache[item.id] = (url, time.time() + 300)
            return url

    def invalidate_dlink(self, item: RemoteItem) -> None:
        """直链被拒（403/31326）时调用，强制下次重新解析。"""
        self._dlink_cache.pop(item.id, None)

    def _download_headers(self) -> dict:
        """下载走的是 d.pcs.baidu.com，网页态必须把 cookie 一起带上，
        否则会报 31045 user not exists。"""
        if self.mode == "cookie":
            return {
                "User-Agent": "netdisk;P2SP;3.0.20.135",
                "Cookie": self.cookie,
                "Referer": "https://pan.baidu.com/disk/home",
            }
        return {"User-Agent": "pan.baidu.com"}

    # 实测（2026-10-25）：百度 d.pcs.baidu.com 对单次 Range 请求有 **4MB 硬上限**。
    #   Range 0-4194303 (4MB)   -> 206 正常
    #   Range 0-6291455 (6MB)   -> 403 {"error_code":31326,"error_msg":"user is not authorized, hitcode:104"}
    # 也就是说 31326 与登录态、权限、风控都无关，纯粹是「一次要太多」。
    # 会员/非会员这条都一样（充值后复测，6MB 仍然 403）。
    #
    # 速度差异极大（同一账号实测）：
    #   非会员：~111KB/s，4 并发各读 1MB 时每条只剩 13~21KB/s，大请求必被掐断
    #   会员  ：单线程 3.97MB/s，4 并发总吞吐 8.45MB/s（6 并发反而降到 7.47）
    # 所以取「4MB 段 + 4 并发」吃满会员带宽；万一是非会员或网络变慢，
    # _read_chunk 会自动把段减半重试，不会卡死。
    # ===== 下载能力声明（基类 SourceAdapter._resumable_read 会用到）=====
    #
    # 实测（2026-10-25，同一账号）：百度 d.pcs.baidu.com 对单次 Range 请求
    # 有 **4MB 硬上限**，与会员无关：
    #   Range 0-4194303 (4MB)   -> 206 正常
    #   Range 0-6291455 (6MB)   -> 403 {"error_code":31326,
    #                                   "error_msg":"user is not authorized, hitcode:104"}
    # 所以 31326 不是登录态/权限/风控问题，纯粹是「一次要太多」。
    #
    # 速度差异极大：
    #   非会员：~111KB/s，4 并发各读 1MB 时每条只剩 13~21KB/s，大请求必被掐断
    #   会员  ：单线程 3.97MB/s，4 并发总吞吐 8.45MB/s（6 并发反而降到 7.47）
    read_max_bytes = 4 * 1024 * 1024
    min_segment = 256 * 1024
    # 同时最多几条下载连接（引擎会按这个值收敛并发，见 engine 里的 MAX_CONCURRENT）
    MAX_CONCURRENT = 4

    # 兼容旧名字（有测试和外部引用在用）
    MAX_RANGE = read_max_bytes
    MIN_SEGMENT = min_segment

    def _prepare_download(self, item: RemoteItem, force_refresh: bool = False) -> tuple[str, dict]:
        """按需（强制）解析直链，并带上网页态必需的请求头。"""
        return self.dlink(item, force=force_refresh), self._download_headers()


class BaiduSource(_BaiduCommon, SourceAdapter):
    kind = "source"
    label = "百度网盘"

    def describe(self) -> dict:
        info = {
            "provider": "baidu",
            "mode": self.mode,
            "authorized": bool(self.cookie if self.mode == "cookie" else self.access_token),
            "account": account_label(self, self.fetch_account),
            "scope_note": (
                "cookie 模式：可读全盘（非公开接口）"
                if self.mode == "cookie"
                else "官方模式：仅可读 /apps/<应用名>/ 目录"
            ),
        }
        # 老版本扫码只存了 BDUSS/STOKEN。百度下载风控会看 BAIDUID，
        # 缺了会偶发 403「31326 user is not authorized」——提前提示用户重扫，
        # 比等传输到一半失败再排查省事。
        if self.mode == "cookie" and self.cookie and "BAIDUID" not in self.cookie:
            info["warning"] = "登录态不完整（缺 BAIDUID），下载可能偶发 403；建议重新扫码登录一次"
        # 界面上要在目录栏标明「哪个网盘的哪个账号」。user_info() 会走网络，
        # 所以每个实例只试一次：成功就带上，失败就算了（不显示账号而已，不能因此让状态刷新变慢或报错）。
        if info["authorized"] and not getattr(self, "_acct_tried", False):
            self._acct_tried = True
            try:
                u = self.user_info()
                name = (u.get("netdisk_name") or u.get("baidu_name") or "").strip()
                if name:
                    info["account"] = name
            except Exception:
                pass
        return info

    def user_info(self) -> dict:
        self.ensure_token()
        if self.mode == "cookie":
            return {"mode": "cookie"}
        query = urllib.parse.urlencode({"method": "userinfo", "access_token": self.access_token})
        data = httpc.get_json(f"{XPAN_BASE}/nas?{query}", headers=self._api_headers())
        if data.get("errno", 0) != 0:
            raise _err(data.get("errno", 0), "获取用户信息")
        return data


class BaiduSink(_BaiduCommon, SinkAdapter):
    """百度网盘作为迁移目标（网页登录态上传）。

    实测出来的规矩（都是踩过的坑）：

    1. 分片固定 **4MB**，跟工具里的 part_size 无关；分片序号就是 4MB 块的下标。
    2. 分片走 `c.pcs.baidu.com/rest/2.0/pcs/superfile2`，**URL 必须带 app_id=250528**，
       否则报 31024 app id is empty；文件内容用 multipart 的 file 字段。
    3. `precreate` 的 block_list 必须数量正确，但**可以先塞占位 md5**（全 0），
       这样就不用为了算哈希把整个文件先下载一遍；真实 md5 在 `create` 时才需要。
    4. 目录已存在时 `api/create` 不会报错，而是**自动改名**（xxx_20261024_224946），
       所以建目录前必须先查存在性，否则会凭空生出一堆垃圾目录。
    5. 0 字节文件走不通（`api/create` 返回 errno=10），明确报错而不是静默失败。
    6. 列表里的 `md5` 字段可能是混淆值（含非 hex 字符），不能用来校验；回读按字节比。
    """
    kind = "sink"
    label = "百度网盘"
    supports_empty_file = False

    BLOCK = 4 * 1024 * 1024
    APP_QS = "clienttype=0&app_id=250528&web=1"
    UPLOAD_HOST = "https://c.pcs.baidu.com"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._made_dirs: set[str] = set()
        # upload_id -> {"path": 目标绝对路径, "md5s": {块序号: md5}}
        self._sessions: dict[str, dict] = {}

    # ---------- 基础 ----------
    def describe(self) -> dict:
        return {
            "provider": "baidu",
            "mode": self.mode,
            "authorized": bool(self.cookie if self.mode == "cookie" else self.access_token),
            "writable": True,
            "account": account_label(self, self.fetch_account),
            "scope_note": (
                "cookie 模式：可写入全盘（非公开接口）"
                if self.mode == "cookie"
                else "官方模式：仅可写入 /apps/<应用名>/ 目录"
            ),
        }

    def _post_form(self, path: str, fields: dict) -> dict:
        self.ensure_token()
        body = urllib.parse.urlencode(fields).encode()
        _, _, raw = httpc.request(
            "POST",
            f"{WEB_BASE}{path}?{self.APP_QS}",
            headers={**self._cookie_headers(), "Content-Type": "application/x-www-form-urlencoded"},
            data=body,
            retries=1,
        )
        return json.loads(raw.decode("utf-8", "replace"))

    def _upload_block(self, path: str, upload_id: str, seq: int, data: bytes) -> str:
        """上传一个 4MB 块，返回服务端确认的 md5。"""
        boundary = "----panmigrate" + uuid.uuid4().hex
        body = b"".join([
            (
                f"--{boundary}\r\n"
                f'Content-Disposition: form-data; name="file"; filename="blk{seq}"\r\n'
                f"Content-Type: application/octet-stream\r\n\r\n"
            ).encode(),
            data,
            f"\r\n--{boundary}--\r\n".encode(),
        ])
        query = urllib.parse.urlencode({
            "method": "upload",
            "type": "tmpfile",
            "path": path,
            "uploadid": upload_id,
            "partseq": seq,
            "app_id": "250528",
            "channel": "chunlei",
            "clienttype": "0",
            "web": "1",
        })
        _, _, raw = httpc.request(
            "POST",
            f"{self.UPLOAD_HOST}/rest/2.0/pcs/superfile2?{query}",
            headers={**self._cookie_headers(), "Content-Type": f"multipart/form-data; boundary={boundary}"},
            data=body,
            timeout=300,
            retries=2,
        )
        info = json.loads(raw.decode("utf-8", "replace") or "{}")
        if info.get("error_code"):
            raise BaiduError(
                f"上传分片 {seq} 失败：{info.get('error_code')} {info.get('error_msg') or ''}".strip()
            )
        return info.get("md5") or ""

    def _invalidate(self) -> None:
        self._dir_cache.clear()

    # ---------- 目录 ----------
    def ensure_dir(self, path: str) -> str:
        path = "/" + (path or "/").strip().strip("/")
        if path == "/":
            return "/"
        current = ""
        for name in [part for part in path.split("/") if part]:
            current += "/" + name
            if current in self._made_dirs:
                continue
            parent = current.rsplit("/", 1)[0] or "/"
            existing = None
            try:
                for entry in self.list_dir(parent):
                    if entry.name == name and entry.is_dir:
                        existing = entry
                        break
            except BaiduError:
                existing = None
            if existing is None:
                resp = self._post_form(
                    "/api/create",
                    {"path": current, "isdir": 1, "block_list": "[]", "size": 0},
                )
                errno = resp.get("errno", 0)
                if errno != 0:
                    raise _err(errno, current)
            self._made_dirs.add(current)
            self._invalidate()
        return path

    def browse(self, path: str) -> list[RemoteItem]:
        path = "/" + (path or "/").strip("/").strip("/")
        items = self.list_dir(path)
        for entry in items:
            entry.path = (path.rstrip("/") + "/" + entry.name) if path != "/" else "/" + entry.name
        return items

    def walk_files(self, path: str) -> dict:
        out: dict[str, tuple[int, RemoteItem]] = {}
        start = "/" + (path or "/").strip().strip("/")
        stack = [(start, "")]
        while stack:
            current, rel = stack.pop()
            try:
                entries = self.list_dir(current)
            except BaiduError:
                continue
            for entry in entries:
                child_rel = (rel + "/" + entry.name).lstrip("/")
                if entry.is_dir:
                    stack.append((current.rstrip("/") + "/" + entry.name, child_rel))
                else:
                    out[child_rel] = (entry.size, entry)
        return out

    def child(self, parent_id: str, name: str) -> RemoteItem | None:
        try:
            for entry in self.list_dir(parent_id or "/"):
                if entry.name == name:
                    return entry
        except BaiduError:
            return None
        return None

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
            raise BaiduError(
                "百度网盘的上传接口不支持 0 字节文件（接口返回 errno=10），这个文件被跳过"
            )
        folder = "/" + (parent_id or "/").strip("/")
        final_name = name
        existing = self.child(folder, name)
        if existing is not None:
            if name_mode == "refuse":
                raise BaiduError(f"目标端已存在同名文件：{name}")
            if name_mode == "auto_rename":
                final_name = self._free_name(folder, name)

        target = (folder.rstrip("/") + "/" + final_name) if folder != "/" else "/" + final_name
        count = max(1, (size + self.BLOCK - 1) // self.BLOCK)
        # 占位 md5：只为拿到 upload_id，真实 md5 在 create 时提交
        resp = self._post_form(
            "/api/precreate",
            {
                "path": target,
                "size": size,
                "isdir": 0,
                "autoinit": 1,
                "rtype": 3,
                "block_list": json.dumps(["0" * 32] * count),
            },
        )
        errno = resp.get("errno", 0)
        if errno != 0:
            raise _err(errno, f"预上传 {final_name}")
        upload_id = resp.get("uploadid") or ""
        if not upload_id:
            raise BaiduError(f"预上传没有返回 upload_id：{str(resp)[:200]}")
        self._sessions[upload_id] = {"path": target, "md5s": {}}
        parts = [
            {"part_number": seq + 1, "upload_url": f"baidu://{upload_id}"}
            for seq in range(count)
        ]
        return UploadSession(
            file_id="",
            upload_id=upload_id,
            parts=parts,
            part_size=self.BLOCK,
            size=size,
            name=final_name,
        )

    def _free_name(self, folder: str, name: str) -> str:
        if "." in name:
            stem, ext = name.rsplit(".", 1)
            suffix = "." + ext
        else:
            stem, suffix = name, ""
        n = 1
        while True:
            candidate = f"{stem} ({n}){suffix}"
            if self.child(folder, candidate) is None:
                return candidate
            n += 1

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        upload_id = upload_url.split("://", 1)[-1]
        session = self._sessions.get(upload_id)
        if not session:
            raise BaiduError("上传会话已丢失，请重新开始这个文件")
        md5 = self._upload_block(session["path"], upload_id, int(part_number) - 1, data)
        session["md5s"][int(part_number) - 1] = md5 or hashlib.md5(data).hexdigest()
        return md5

    def list_uploaded(self, session: UploadSession) -> list[int]:
        """百度没有「查询已传块」的接口，跨进程续传只能靠本地任务库记录。"""
        return []

    def complete(self, session: UploadSession) -> str:
        info = self._sessions.get(session.upload_id)
        if not info:
            raise BaiduError("上传会话已丢失，请重新开始这个文件")
        count = max(1, (session.size + self.BLOCK - 1) // self.BLOCK)
        missing = [seq for seq in range(count) if seq not in info["md5s"]]
        if missing:
            raise BaiduError(f"还有 {len(missing)} 个分片没传完，不能合并")
        block_list = [info["md5s"][seq] for seq in range(count)]
        resp = self._post_form(
            "/api/create",
            {
                "path": info["path"],
                "size": session.size,
                "isdir": 0,
                "rtype": 3,
                "uploadid": session.upload_id,
                "block_list": json.dumps(block_list),
            },
        )
        errno = resp.get("errno", 0)
        if errno != 0:
            raise _err(errno, f"合并 {session.name or ''}".strip())
        self._sessions.pop(session.upload_id, None)
        self._invalidate()
        return str(resp.get("fs_id") or "")

    def get_file(self, parent_id: str, name: str) -> RemoteItem | None:
        return self.child(parent_id, name)

    def quota(self) -> dict | None:
        """容量：百度直接给了 total/used。

        注意它的 `free` 字段实测不可信（等于 total），所以剩余空间自己算。
        """
        try:
            resp = httpc.get_json(
                f"{WEB_BASE}/api/quota?"
                + urllib.parse.urlencode({"checkfree": 1, "checkexpire": 1}),
                headers=self._cookie_headers(),
                retries=1,
            )
        except Exception:
            return None
        if resp.get("errno"):
            return None
        total = int(resp.get("total") or 0)
        used = int(resp.get("used") or 0)
        if total <= 0:
            return None
        return {"total": total, "used": used, "free": max(0, total - used)}

