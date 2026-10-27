"""115 网盘适配器（网页 Cookie 登录态）—— 目前只做「源端」。

为什么先只做源端：
  115 的读取侧很干净：列目录一个 GET、拿直链一个带签名的 POST，cookie 就够。
  写入侧要复杂得多 —— `uplb.115.com/3.0/initupload.php` 需要 `sig` 和 `token`
  两个签名，其中 sig 依赖 cookie 里的 `userkey`、token 还牵扯服务端下发的
  sign_key/sign_val，参数一变就全线失效；上传本身还要接阿里云 OSS 的
  callback 回跳。这部分没有真实账号反复试是调不出来的，硬写等于交付一个
  看着能跑、实际必报错的适配器，所以先留空，等有账号再补。

地址规律（按 cid 寻址，根目录 cid = "0"）：
  列表   GET  https://webapi.115.com/files
              ?aid=1&cid=<id>&o=user_ptime&asc=0&offset=0&show_dir=1
              &limit=100&snap=0&natsort=1&record_open_time=1&source=
              &format=json&star=0&stem=0&suffix=
  建目录 POST https://webapi.115.com/files/add      {pid, cname}
  直链   POST https://webapi.115.com/files/download {pickcode, type:0, sign, time}
          sign = MD5(f"download{pickcode}{time}salt!@#") 的大写十六进制

关键坑（务必保留）：
  1. 目录项靠 `cid` 字段区分：有 cid（且不为 0）是目录，`cid` 为空才是文件。
     很多实现误用 fid 递归，结果进不了子目录。
  2. 列表是 offset 分页，不是页码；返回的 count 是总数。
  3. 下载直链绑 User-Agent：用什么 UA 换的链，就得用什么 UA 去下，
     否则可能 403。这里换链和下载统一用同一个 UA。
  4. `state: false` 时看 errno：911 是登录态失效（要重新粘 cookie），
     别当网络错误重试。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import time
import urllib.parse
from binascii import b2a_hex, crc32
from email.utils import formatdate
from typing import Iterator

from .. import crypto
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

WEB = "https://webapi.115.com"
ROOT_ID = "0"
UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)
# 直链签名用的固定盐值（前端 JS 里写死的那串）
SIGN_SALT = "salt!@#"
# 115 目录项里用 cid 表示目录，单个目录最多一次拉 100 条
PAGE_SIZE = 100


class Pan115Error(AdapterError):
    pass


class Pan115AuthError(Pan115Error, AuthError):
    """登录态缺失/失效：重试无意义，要提示用户重新粘 cookie。"""

    retryable = False


class _Pan115Common:
    def __init__(self, cfg: dict):
        self.cfg = cfg or {}
        self.cookie = (self.cfg.get("cookie") or "").strip()
        self._children: dict[str, list[RemoteItem]] = {}
        self._path_ids: dict[str, str] = {"": ROOT_ID, "/": ROOT_ID}
        # 换直链时服务端会 Set-Cookie（一个绑定该文件路径的动态 cookie），
        # 下载 CDN 必须带上它，否则 403「no cookie value」
        self._dl_cookie: dict[str, str] = {}

    def fetch_account(self) -> str:
        """115 的 /user/info 会把显示名放在顶层 data 里（脱敏手机号，如 158*****728）。"""
        resp = httpc.get_json(f"{WEB}/user/info", headers=self._headers(), retries=1)
        return str(resp.get("data") or "") if isinstance(resp.get("data"), str) else ""

    def describe(self) -> dict:
        return {
            "provider": "pan115",
            "mode": "cookie",
            "authorized": bool(self.cookie),
            "account": account_label(self, self.fetch_account),
            "scope_note": "网页 Cookie 登录：可读写（写入单文件上限 500MB）",
        }

    def _headers(self, json_body: bool = False) -> dict:
        h = {
            "Cookie": self.cookie,
            "User-Agent": UA,
            "Referer": "https://115.com/",
            "Accept": "application/json, text/plain, */*",
        }
        if json_body:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        return h

    def ensure_auth(self) -> None:
        if not self.cookie:
            raise Pan115AuthError(
                "还没登录 115 网盘：请在浏览器打开 115.com 登录，"
                "然后按 F12 → Network 里任一请求，把整条 Cookie 复制到「凭据」里"
            )

    def _raise_for_state(self, resp: dict, what: str) -> None:
        """115 用 state/errno 表达业务错误，HTTP 层永远是 200。"""
        if resp.get("state") is True or resp.get("state") == 1:
            return
        errno = resp.get("errno") or resp.get("errcode") or ""
        message = resp.get("message") or resp.get("error") or ""
        if str(errno) in ("911", "401", "105"):
            raise Pan115AuthError(
                f"115 登录态已失效（errno {errno}）：请重新在浏览器登录后复制新的 Cookie"
            )
        raise Pan115Error(f"115 {what}失败（errno {errno}）：{message or str(resp)[:200]}")

    def _get(self, path: str, query: dict) -> dict:
        self.ensure_auth()
        url = f"{WEB}{path}?{urllib.parse.urlencode(query)}"
        try:
            _, _, raw = httpc.request("GET", url, headers=self._headers(), timeout=60, retries=2)
        except HttpError as exc:
            raise Pan115Error(f"115 接口 {path} 失败（HTTP {exc.status}）：{exc.body[:200]}") from None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise Pan115Error(f"115 接口 {path} 返回非 JSON：{raw.decode('utf-8', 'replace')[:200]}") from None

    def _post(self, path: str, form: dict) -> dict:
        self.ensure_auth()
        body = urllib.parse.urlencode(form).encode("utf-8")
        try:
            _, _, raw = httpc.request(
                "POST",
                f"{WEB}{path}",
                headers=self._headers(json_body=True),
                data=body,
                timeout=60,
                retries=2,
            )
        except HttpError as exc:
            raise Pan115Error(f"115 接口 {path} 失败（HTTP {exc.status}）：{exc.body[:200]}") from None
        try:
            return json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise Pan115Error(f"115 接口 {path} 返回非 JSON：{raw.decode('utf-8', 'replace')[:200]}") from None

    # ---------- 目录 ----------
    def list_folder(self, folder_id: str, force: bool = False) -> list[RemoteItem]:
        folder_id = str(folder_id or ROOT_ID)
        if not force and folder_id in self._children:
            return self._children[folder_id]
        items: list[RemoteItem] = []
        offset = 0
        total = None
        for _ in range(2000):
            resp = self._get("/files", {
                "aid": 1,
                "cid": folder_id,
                "o": "user_ptime",
                "asc": 0,
                "offset": offset,
                "show_dir": 1,
                "limit": PAGE_SIZE,
                "snap": 0,
                "natsort": 1,
                "record_open_time": 1,
                "source": "",
                "format": "json",
                "star": 0,
                "stem": 0,
                "suffix": "",
            })
            self._raise_for_state(resp, "列目录")
            batch = resp.get("data") or []
            if total is None:
                total = int(resp.get("count") or 0)
            for raw in batch:
                name = raw.get("n") or raw.get("name") or ""
                # 目录项的 cid 有值；文件项 cid 为空串。用 cid 判目录最可靠
                cid = raw.get("cid")
                is_dir = bool(cid) and str(cid) != "0"
                pickcode = raw.get("pc") or ""
                items.append(
                    RemoteItem(
                        # 文件夹存 cid（继续往下钻要用它），文件存 pickcode（换直链要用它）
                        id=str(cid) if is_dir else pickcode,
                        name=name,
                        is_dir=is_dir,
                        size=int(raw.get("s") or 0),
                        sha1=(raw.get("sha") or ""),   # 115 的字段名是 sha，不是 sha1
                        modified=str(raw.get("te") or raw.get("t") or ""),
                    )
                )
            offset += len(batch)
            if not batch or (total is not None and offset >= total):
                break
        self._children[folder_id] = items
        return items

    def resolve_path(self, path: str) -> str:
        """路径 → cid（逐级按名字找，找不到报错，不自动建）。"""
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
                raise Pan115Error(f"115 目录不存在：{walked}")
            current = found.id
            self._path_ids[walked] = current
        return current

    def list_dir(self, path: str) -> list[RemoteItem]:
        path = "/" + (path or "/").strip().strip("/")
        folder_id = self.resolve_path(path)
        items = self.list_folder(folder_id)
        for entry in items:
            entry.path = (path.rstrip("/") + "/" + entry.name) if path != "/" else "/" + entry.name
        return items

    # ---------- 下载 ----------
    @staticmethod
    def sign_download(pickcode: str, ts: int | None = None) -> tuple[str, int]:
        """直链接口要求的 sign：MD5('download' + pickcode + 秒级时间戳 + 盐) 大写。"""
        stamp = int(ts if ts is not None else time.time())
        raw = f"download{pickcode}{stamp}{SIGN_SALT}"
        return hashlib.md5(raw.encode()).hexdigest().upper(), stamp

    def _download_info(self, pickcode: str) -> tuple[dict, str]:
        """GET /files/download，返回 (响应 JSON, Set-Cookie 头)。

        抽成单独方法是为了可测：测试覆盖它就能绕开网络。
        """
        self.ensure_auth()
        query = urllib.parse.urlencode({"pickcode": pickcode, "type": 0})
        try:
            _, headers, raw = httpc.request(
                "GET", f"{WEB}/files/download?{query}",
                headers=self._headers(), timeout=30, retries=1,
            )
        except HttpError as exc:
            raise Pan115Error(f"115 换直链失败（HTTP {exc.status}）：{exc.body[:200]}") from None
        try:
            resp = json.loads(raw.decode("utf-8", "replace"))
        except json.JSONDecodeError:
            raise Pan115Error(
                f"115 换直链返回非 JSON：{raw.decode('utf-8', 'replace')[:200]}"
            ) from None
        return resp, (headers.get("Set-Cookie") or headers.get("set-cookie") or "")

    def download_url(self, pickcode: str) -> str:
        """换取下载直链。

        真机实测（2026-10-26）：这个接口**只能用 GET**，POST 会一律回
        「参数错误 200900」；而且**不需要 sign/timestamp 签名**，只要 pickcode。
        直链在响应的**顶层 `file_url`**，不在 data 里。
        """
        resp, set_cookie = self._download_info(pickcode)
        self._raise_for_state(resp, "获取下载直链")
        # 关键：服务端会把「这个文件专用」的 cookie 放在 Set-Cookie 里，
        # CDN 校验的就是它（实测不带就 403 no cookie value）
        if set_cookie:
            pair = set_cookie.split(";", 1)[0].strip()
            if "=" in pair:
                self._dl_cookie[str(pickcode)] = pair
        url = (
            resp.get("file_url")
            or (resp.get("data") or {}).get("url")
            or resp.get("url")
            or ""
        )
        if not url:
            raise Pan115Error(f"115 没有返回下载直链：{str(resp)[:200]}")
        return url


class Pan115Source(_Pan115Common, SourceAdapter):
    kind = "source"
    label = "115 网盘"

    read_max_bytes = 4 * 1024 * 1024
    MAX_CONCURRENT = 4

    def walk_files(self, path: str, on_progress=None) -> Iterator[RemoteItem]:
        yield from self._walk("/" + (path or "/").strip("/"), "", on_progress)

    def _prepare_download(self, item: RemoteItem, force_refresh: bool = False) -> tuple[str, dict]:
        # 两个都实测过：
        #   1) 直链和 UA 绑定，换链用的 UA 必须和下载用的完全一致
        #   2) CDN 还要校验登录态，不带 Cookie 会 403「no cookie」
        url = self.download_url(item.id)
        ck = self._dl_cookie.get(str(item.id))
        return url, {
            "User-Agent": UA,
            "Referer": "https://115.com/",
            # 登录 cookie + 服务端为这个文件下发的下载 cookie，两个都要
            "Cookie": f"{self.cookie}; {ck}" if ck else self.cookie,
        }


# ============================================================================
# 写入侧（Sink）：4.0 initupload + 阿里云 OSS 直传
#
# 真机验证过的完整链路（2026-10-26）：
#   1. GET  webapi.115.com/files/uploadinfo     → user_id + userkey
#   2. GET  uplb.115.com/3.0/gettoken.php       → OSS 临时凭证（STS）
#   3. POST uplb.115.com/4.0/initupload.php?k_ec=...
#      请求体必须是 **AES-128-CBC 加密的 urlencode 串**，且
#      **Content-Type 必须是 application/x-www-form-urlencoded**
#      （用 octet-stream 会被判「userid/filesize/target/pickcode invalid」）
#   4. PUT  https://{bucket}.oss-cn-shenzhen.aliyuncs.com/{object}
#      带 OSS V1 签名 + x-oss-callback / x-oss-callback-var
#   5. OSS 回调 completeupload.php 自动在 115 建好文件，返回 pick_code
#
# 几个反直觉的点，改代码前务必看：
#   - 名字里带 ECDH，但**没有任何椭圆曲线运算**：就是固定 key/iv 的 AES-CBC
#   - 响应是加密的，且外面还套了 lz4 分块（[2字节长度][lz4块] 重复）
#   - 115 是**整文件上传**（OSS PutObject），不支持分片；因此单文件有大小上限
#   - 它的预上传要 **SHA1**，不是 MD5
# ============================================================================

# 单文件上限：整文件要进内存，太大会把进程撑爆
MAX_UPLOAD_BYTES = 500 * 1024 * 1024

_APP_VERSION = "36.2.28"
_APP_UA = (
    f"Mozilla/5.0 115disk/{_APP_VERSION} 115Browser/{_APP_VERSION} "
    f"115wangpan_android/{_APP_VERSION}"
)
_CRC_SALT = b"^j>WD3Kr?J2gLFjD4W2y@"
_MD5_SALT = b"Qclm8MGWUv59TnrR0XPg"
_AES_PUBKEY = bytes.fromhex("1d030e80a178dceececda377de128d8ed9ddcf55ae61ed46ea121a1cfc81")
_AES_KEY = bytes.fromhex("fb1a19d652f5aaf7bc651d0f69bf422f")
_AES_IV = bytes.fromhex("69bf422f49960550a0ad44ec3446cb4c")
_OSS_ENDPOINT = "oss-cn-shenzhen.aliyuncs.com"


def _lz4_block(src: bytes) -> bytes:
    out = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1
        lit = token >> 4
        if lit == 15:
            while i < n:
                b = src[i]
                i += 1
                lit += b
                if b != 255:
                    break
        if lit:
            out += src[i: i + lit]
            i += lit
        if i >= n:
            break
        off = src[i] | (src[i + 1] << 8)
        i += 2
        if off == 0:
            break
        mlen = token & 0x0F
        if mlen == 15:
            while i < n:
                b = src[i]
                i += 1
                mlen += b
                if b != 255:
                    break
        mlen += 4
        start = len(out) - off
        for k in range(mlen):
            out.append(out[start + k])
    return bytes(out)


def _lz4_decompress(src: bytes) -> bytes:
    """115 的响应格式：[2字节小端长度][lz4块]，重复若干次。"""
    data = b""
    while len(src) > 2:
        n = src[0] | (src[1] << 8)
        if n == 0:
            break
        data += _lz4_block(src[2: 2 + n])
        src = src[2 + n:]
    return data


def _make_k_ec(ts: int) -> str:
    """URL 上要带的 k_ec：公钥片段 + 时间戳 + crc32，纯拼接 + base64。"""
    tk = bytearray(_AES_PUBKEY[:15] + b"\x00s\x00\x00\x00")
    tk += ts.to_bytes(4, "little") + _AES_PUBKEY[15:] + b"\x00\x01\x00\x00\x00"
    tk += crc32(_CRC_SALT + bytes(tk)).to_bytes(4, "little")
    return base64.b64encode(bytes(tk)).decode()


def _oss_sign(token: dict, method: str, bucket: str, key: str, headers: dict) -> dict:
    """OSS V1 签名（HMAC-SHA1 + base64），不是 SigV4。"""
    h = {k.lower(): v for k, v in headers.items()}
    h["x-oss-security-token"] = token["SecurityToken"]
    date = h.setdefault("date", formatdate(usegmt=True))
    oss_headers = "\n".join(
        f"{k}:{v}" for k, v in sorted(h.items()) if k.startswith("x-oss-")
    )
    base = (
        f"{method.upper()}\n{h.get('content-md5', '')}\n"
        f"{h.get('content-type', '')}\n{date}\n{oss_headers}\n/{bucket}/{key}"
    )
    sig = base64.b64encode(
        hmac.new(token["AccessKeySecret"].encode(), base.encode(), "sha1").digest()
    ).decode()
    h["authorization"] = f"OSS {token['AccessKeyId']}:{sig}"
    return h


class Pan115Sink(_Pan115Common, SinkAdapter):
    """115 网盘作为目标端（整文件上传，单文件上限 500MB）。"""

    kind = "sink"
    label = "115 网盘"
    supports_empty_file = True
    max_name_bytes = 255
    sequential_parts = False
    # 115 的预上传强制要整文件 SHA1，而 123/百度 的列表只有 MD5，只能自己算
    needs_sha1 = True
    # 整文件上传（不分片），超过这个大小直接跳过，不等传一半才失败
    max_file_bytes = MAX_UPLOAD_BYTES
    upload_limit_hint = "它是整文件上传，不分片"

    def __init__(self, cfg: dict):
        super().__init__(cfg)
        self._ctx: dict = {}          # user_id / userkey / sts
        self._ctx_at: float = 0.0
        self._sessions: dict[str, dict] = {}

    # ---------- 上传上下文 ----------
    def _headers(self, json_body: bool = False) -> dict:
        """上传流程用的请求头。

        必须和父类同签名：父类的 _post 会用 _headers(json_body=True) 调动，
        少了这个参数，115 当目标端建目录时直接 TypeError。
        """
        h = {
            "Cookie": self.cookie,
            "User-Agent": _APP_UA,
            "Referer": "https://115.com/",
            "Accept": "application/json, text/plain, */*",
        }
        if json_body:
            h["Content-Type"] = "application/x-www-form-urlencoded"
        return h

    def _upload_ctx(self) -> dict:
        """拿 user_id + userkey + OSS STS 凭证（缓存 30 分钟）。"""
        if self._ctx and time.time() - self._ctx_at < 1800:
            return self._ctx
        self.ensure_auth()
        info = httpc.get_json(
            "https://webapi.115.com/files/uploadinfo", headers=self._headers(), retries=1
        )
        uid = str(info.get("user_id") or "")
        userkey = str(info.get("userkey") or "")
        if not (uid and userkey):
            raise Pan115AuthError("115 没有返回 userkey，登录态可能已失效，请重新扫码")
        tok = httpc.get_json(
            "https://uplb.115.com/3.0/gettoken.php", headers=self._headers(), retries=1
        )
        if not tok.get("AccessKeyId"):
            raise Pan115Error(f"115 没有返回 OSS 上传凭证：{str(tok)[:200]}")
        self._ctx = {"user_id": uid, "userkey": userkey, "sts": tok}
        self._ctx_at = time.time()
        return self._ctx

    # ---------- 目录 ----------
    def walk_files(self, path: str) -> dict:
        out: dict[str, tuple[int, RemoteItem]] = {}
        start = "/" + (path or "/").strip().strip("/")
        stack = [(start, "")]
        while stack:
            current, rel = stack.pop()
            try:
                entries = self.list_dir(current)
            except Pan115Error:
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
                resp = self._post("/files/add", {"pid": current, "cname": name})
                self._raise_for_state(resp, "建目录")
                current = str(resp.get("cid") or "")
                if not current:
                    raise Pan115Error(f"115 建目录没有返回 cid：{str(resp)[:200]}")
            self._path_ids[walked] = current
            self._children.pop(current, None)
        return current

    def child(self, parent_id: str, name: str) -> RemoteItem | None:
        for entry in self.list_folder(parent_id):
            if entry.name == name:
                return entry
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
        if int(size) > MAX_UPLOAD_BYTES:
            raise Pan115Error(
                f"115 云盘是整文件上传（不分片），单个文件最大支持 "
                f"{MAX_UPLOAD_BYTES // 1024 // 1024}MB，这个文件有 "
                f"{int(size) // 1024 // 1024}MB，暂时传不了"
            )
        existing = self.child(parent_id, name)
        if existing is not None and name_mode == "refuse":
            raise Pan115Error(f"目标端已存在同名文件：{name}")
        if not sha1:
            raise Pan115Error(
                "源端没有提供这个文件的 SHA1，115 云盘无法上传"
                "（它的预上传要求整文件 SHA1，和 MD5 不能互推）"
            )

        ctx = self._upload_ctx()
        uid, userkey = ctx["user_id"], ctx["userkey"]
        target = f"U_1_{parent_id or 0}"
        sha1 = sha1.upper()
        ts = int(time.time())

        h = hashlib.sha1(userkey.encode())
        h.update(b2a_hex(hashlib.sha1(f"{uid}{sha1}{target}0".encode()).digest()))
        h.update(b"000000")
        sig = h.hexdigest().upper()

        payload = {
            "appid": "0",
            "appversion": _APP_VERSION,
            "target": target,
            "topupload": "true",
            "fileid": sha1,
            "filename": name,
            "filesize": str(size),
            "userid": uid,
            "userkey": userkey,
            "sign_key": "",
            "sign_val": "",
            "t": str(ts),
            "sig": sig,
        }
        m = hashlib.md5(_MD5_SALT)
        m.update(f"{sha1}{size}{uid}{ts}".encode())
        m.update(b2a_hex(hashlib.md5(uid.encode()).digest()))
        m.update(_APP_VERSION.encode())
        payload["token"] = m.hexdigest()

        raw = urllib.parse.urlencode(
            sorted((k, v) for k, v in payload.items() if v)
        ).encode("latin-1")
        body = crypto.aes_cbc_encrypt(_AES_KEY, _AES_IV, raw)
        try:
            _, _, resp = httpc.request(
                "POST",
                f"https://uplb.115.com/4.0/initupload.php?k_ec={_make_k_ec(ts)}",
                headers={**self._headers(),
                         "Content-Type": "application/x-www-form-urlencoded"},
                data=body,
                timeout=60,
                retries=1,
            )
        except HttpError as exc:
            raise Pan115Error(f"115 预上传失败（HTTP {exc.status}）：{exc.body[:200]}") from None

        try:
            data = json.loads(
                _lz4_decompress(crypto.aes_cbc_decrypt(_AES_KEY, _AES_IV, resp))
                .decode("utf-8", "replace")
            )
        except Exception:
            raise Pan115Error("115 预上传返回无法解析（加密格式可能已变）") from None

        # 注意 0 or "" == ""，所以要显式判断 None，不能用 or 兜底
        _code = data.get("statuscode")
        code = "0" if _code is None else str(_code)
        if code != "0":
            raise Pan115Error(
                f"115 预上传被拒（statuscode={code}）：{data.get('statusmsg') or ''}"
            )
        _st = data.get("status")
        status = "1" if _st is None else str(_st)

        if status == "2":
            # 秒传命中：文件已经建好，不用传字节
            return UploadSession(
                file_id=str(data.get("pickcode") or ""),
                upload_id="",
                parts=[],
                part_size=int(size) or part_size,
                size=size,
                rapid=True,
                name=name,
            )

        object_key = str(data.get("object") or "")
        if not object_key:
            raise Pan115Error(f"115 预上传没有返回 object：{str(data)[:200]}")
        upload_id = str(data.get("pickcode") or object_key)
        self._sessions[upload_id] = {
            "bucket": data.get("bucket") or "fhnfile",
            "object": object_key,
            "callback": data.get("callback") or {},
            "sts": ctx["sts"],
        }
        # 整文件一片（115 不支持分片）
        return UploadSession(
            file_id=upload_id,
            upload_id=upload_id,
            parts=[{"part_number": 1, "upload_url": f"115oss://{upload_id}"}],
            part_size=int(size) or part_size,
            size=size,
            name=name,
        )

    def put_part(self, upload_url: str, data: bytes, part_number: int) -> str:
        upload_id = upload_url.split("://", 1)[-1]
        info = self._sessions.get(upload_id)
        if not info:
            raise Pan115Error("115 上传会话已丢失，请重新开始这个文件")

        def _b64_cb(v) -> str:
            # initupload 返回的 callback / callback_var 本身就是 JSON 字符串，
            # 直接 base64；再 json.dumps 一次会多一层转义，OSS 会报
            # "The callback configuration is not json format"
            if isinstance(v, str):
                return base64.b64encode(v.encode()).decode()
            return base64.b64encode(json.dumps(v or {}).encode()).decode()

        headers = {
            "content-type": "application/octet-stream",
            "x-oss-callback": _b64_cb(info["callback"].get("callback")),
            "x-oss-callback-var": _b64_cb(info["callback"].get("callback_var")),
        }
        signed = _oss_sign(info["sts"], "PUT", info["bucket"], info["object"], headers)
        url = f"https://{info['bucket']}.{_OSS_ENDPOINT}/{info['object']}"
        try:
            _, _, resp = httpc.request(
                "PUT", url, headers=signed, data=data, timeout=600, retries=2
            )
        except HttpError as exc:
            raise Pan115Error(f"115 文件上传失败（HTTP {exc.status}）：{exc.body[:200]}") from None
        # 回调响应里有 pick_code 和 file_id，说明服务端已经收下并建好文件
        try:
            cb = json.loads(resp.decode("utf-8", "replace"))
            if not cb.get("state"):
                raise Pan115Error(f"115 回调失败：{str(cb)[:200]}")
            fid = str((cb.get("data") or {}).get("file_id") or "")
            if fid:
                self._sessions[upload_id]["file_id"] = fid
        except json.JSONDecodeError:
            pass
        return hashlib.md5(data).hexdigest()

    def complete(self, session: UploadSession) -> str:
        if session.rapid:
            return session.file_id
        info = self._sessions.pop(session.upload_id, None) or {}
        self._children.clear()
        return str(info.get("file_id") or session.file_id)

    def read_range(self, item: RemoteItem, offset: int, length: int, on_progress=None) -> bytes:
        """回读目标端字节，供抽样校验。"""
        url = self.download_url(item.id)
        _, _, body = httpc.request(
            "GET", url,
            headers={"User-Agent": UA, "Referer": "https://115.com/",
                     "Range": f"bytes={offset}-{offset + length - 1}",
                     "Accept-Encoding": "identity"},
            timeout=120, retries=2,
        )
        if on_progress and body:
            on_progress(len(body))
        return body

    def get_file(self, parent_id: str, name: str) -> RemoteItem | None:
        return self.child(parent_id, name)
