"""极简 HTTP 客户端：带重试、指数退避、连接复用。

只用标准库，避免任何 pip 安装环节。Windows 上 `python -m panmigrate` 即可跑。
"""
from __future__ import annotations

import http.cookiejar
import json
import random
import socket
import time
import urllib.error
import urllib.parse
import urllib.request

DEFAULT_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

RETRY_STATUS = {408, 429, 500, 502, 503, 504}


class HttpError(Exception):
    def __init__(self, status: int, url: str, body: str = ""):
        self.status = status
        self.url = url
        self.body = body
        super().__init__(f"HTTP {status} <- {url} :: {body[:400]}")

    @property
    def retryable(self) -> bool:
        return self.status in RETRY_STATUS


def _decode(raw: bytes) -> str:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return raw.decode("gbk", "replace")


def request(
    method: str,
    url: str,
    headers: dict | None = None,
    data: bytes | None = None,
    timeout: float = 60.0,
    retries: int = 3,
    backoff: float = 1.6,
) -> tuple[int, dict, bytes]:
    """返回 (status, headers, body)。重试耗尽后抛 HttpError / URLError。"""
    hdrs = {"User-Agent": DEFAULT_UA, "Accept-Encoding": "identity"}
    if headers:
        hdrs.update(headers)

    last_exc: Exception | None = None
    for attempt in range(retries + 1):
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.status, dict(resp.headers), resp.read()
        except urllib.error.HTTPError as exc:
            body = ""
            try:
                body = _decode(exc.read())
            except Exception:
                pass
            err = HttpError(exc.code, url, body)
            if not err.retryable or attempt >= retries:
                raise err from None
            last_exc = err
        except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
            if attempt >= retries:
                raise
            last_exc = exc

        time.sleep(min(backoff ** attempt, 12) + random.uniform(0, 0.4))

    raise last_exc if last_exc else RuntimeError("request failed without exception")


def get_json(url: str, headers: dict | None = None, **kw) -> dict:
    _, _, body = request("GET", url, headers=headers, **kw)
    text = _decode(body)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HttpError(200, url, "响应不是合法 JSON: " + text[:300]) from None


def post_json(url: str, payload: dict, headers: dict | None = None, **kw) -> dict:
    hdrs = {"Content-Type": "application/json"}
    if headers:
        hdrs.update(headers)
    body = json.dumps(payload).encode("utf-8")
    _, _, raw = request("POST", url, headers=hdrs, data=body, **kw)
    text = _decode(raw)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        raise HttpError(200, url, "响应不是合法 JSON: " + text[:300]) from None


def get_bytes(url: str, headers: dict | None = None, byte_range: str | None = None, **kw) -> bytes:
    hdrs = dict(headers or {})
    if byte_range:
        hdrs["Range"] = f"bytes={byte_range}"
    _, _, body = request("GET", url, headers=hdrs, **kw)
    return body


def quote_path(path: str) -> str:
    return urllib.parse.quote(path, safe="")


class Session:
    """带 cookie 的会话。

    扫码登录这类流程必须保持服务端下发的 cookie（百度的 BAIDUID / BDUSS 就靠它），
    普通的一次性请求拿不到，所以单独封一层。
    """

    def __init__(self, headers: dict | None = None):
        self.jar = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.jar)
        )
        self.headers = {
            "User-Agent": DEFAULT_UA,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
        }
        if headers:
            self.headers.update(headers)

    def request(
        self,
        method: str,
        url: str,
        data: bytes | None = None,
        headers: dict | None = None,
        timeout: float = 30.0,
        retries: int = 2,
    ) -> tuple[int, dict, bytes]:
        hdrs = dict(self.headers)
        if headers:
            hdrs.update(headers)
        last: Exception | None = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
            try:
                with self.opener.open(req, timeout=timeout) as resp:
                    return resp.status, dict(resp.headers), resp.read()
            except urllib.error.HTTPError as exc:
                body = ""
                try:
                    body = _decode(exc.read())
                except Exception:
                    pass
                err = HttpError(exc.code, url, body)
                if not err.retryable or attempt >= retries:
                    raise err from None
                last = err
            except (urllib.error.URLError, socket.timeout, ConnectionError, OSError) as exc:
                if attempt >= retries:
                    raise
                last = exc
            time.sleep(min(2.0 ** attempt, 8) + random.uniform(0, 0.3))
        raise last or RuntimeError("request failed without exception")

    def get_bytes(self, url: str, headers: dict | None = None, **kw) -> bytes:
        _, _, body = self.request("GET", url, headers=headers, **kw)
        return body

    def get_text(self, url: str, headers: dict | None = None, **kw) -> str:
        return _decode(self.get_bytes(url, headers=headers, **kw))

    def get_json(self, url: str, headers: dict | None = None, **kw) -> dict:
        text = self.get_text(url, headers=headers, **kw)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise HttpError(200, url, "响应不是合法 JSON：" + text[:300]) from None

    def post_form(
        self,
        url: str,
        fields: dict,
        headers: dict | None = None,
        **kw,
    ) -> dict:
        body = urllib.parse.urlencode(fields).encode("utf-8")
        hdrs = {"Content-Type": "application/x-www-form-urlencoded"}
        if headers:
            hdrs.update(headers)
        _, _, raw = self.request("POST", url, data=body, headers=hdrs, **kw)
        text = _decode(raw)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            raise HttpError(200, url, "响应不是合法 JSON：" + text[:300]) from None

    def cookie(self, name: str) -> str:
        for item in self.jar:
            if item.name == name:
                return item.value or ""
        return ""

    def cookie_header(self) -> str:
        return "; ".join(f"{c.name}={c.value}" for c in self.jar)

