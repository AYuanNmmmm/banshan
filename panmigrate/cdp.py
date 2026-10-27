"""极小 CDP（Chrome DevTools Protocol）客户端，纯标准库实现。

用途：天翼云盘的登录 cookie 是 app-bound 加密（DPAPI），离线解不开，只能从
活着的浏览器进程经 CDP 读出来。早先这条路靠 `node web/tylogin.js` 跑，
换到一台没装 node 的电脑上就用不了 —— 而这是个「双击就能用」的小工具，
不该要求用户先装 Node.js。

这里用 socket 手写 WebSocket 握手续传 + 帧编解码，够覆盖 CDP 用到的场景：
  - 文本帧（含分片）
  - ping/pong（浏览器会主动发，不回会被断开）
  - 请求/响应按 id 配对，事件消息自动跳过
"""
from __future__ import annotations

import base64
import json
import os
import socket
import struct
import threading
import urllib.parse
import urllib.request

__all__ = ["WebSocket", "http_json", "CDPError"]


class CDPError(RuntimeError):
    pass


def http_json(port: int, path: str = "/json/version", timeout: float = 5.0) -> dict | list:
    """读调试端口的 JSON 接口（/json/version、/json/list）。"""
    url = f"http://127.0.0.1:{port}{path}"
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8", "replace"))


class WebSocket:
    """够用的 WebSocket 客户端：文本帧 + ping/pong + 分片重组。"""

    def __init__(self, url: str, timeout: float = 10.0):
        parsed = urllib.parse.urlparse(url)
        if parsed.scheme not in ("ws", "http"):
            raise CDPError(f"不支持的调试地址：{url}")
        host = parsed.hostname or "127.0.0.1"
        port = parsed.port or 80
        path = parsed.path or "/"
        if parsed.query:
            path += "?" + parsed.query

        self.sock = socket.create_connection((host, port), timeout=timeout)
        self.sock.settimeout(timeout)
        self._buf = b""
        self._lock = threading.Lock()

        key = base64.b64encode(os.urandom(16)).decode("ascii")
        handshake = (
            f"GET {path} HTTP/1.1\r\n"
            f"Host: {host}:{port}\r\n"
            "Upgrade: websocket\r\n"
            "Connection: Upgrade\r\n"
            f"Sec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n"
            "Origin: http://127.0.0.1\r\n"
            "\r\n"
        )
        self.sock.sendall(handshake.encode("ascii"))

        # 读到响应头结束位置；剩下的字节属于第一帧，留着别丢
        head = b""
        while b"\r\n\r\n" not in head:
            chunk = self.sock.recv(4096)
            if not chunk:
                raise CDPError("WebSocket 握手失败：连接被关闭")
            head += chunk
            if len(head) > 65536:
                raise CDPError("WebSocket 握手失败：响应头过大")
        header, _, rest = head.partition(b"\r\n\r\n")
        status_line = header.split(b"\r\n", 1)[0].decode("latin1", "replace")
        if " 101 " not in status_line:
            raise CDPError(f"WebSocket 握手被拒：{status_line[:120]}")
        self._buf = rest

    # ---------- 底层帧 ----------
    def _read_exact(self, n: int) -> bytes:
        while len(self._buf) < n:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise CDPError("WebSocket 连接已断开")
            self._buf += chunk
        out, self._buf = self._buf[:n], self._buf[n:]
        return out

    def _read_frame(self) -> tuple[int, bool, bytes]:
        b0, b1 = self._read_exact(2)
        fin = bool(b0 & 0x80)
        opcode = b0 & 0x0F
        masked = bool(b1 & 0x80)
        length = b1 & 0x7F
        if length == 126:
            (length,) = struct.unpack("!H", self._read_exact(2))
        elif length == 127:
            (length,) = struct.unpack("!Q", self._read_exact(8))
        mask = self._read_exact(4) if masked else b""
        payload = self._read_exact(length) if length else b""
        if masked and payload:
            payload = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        return opcode, fin, payload

    def _write_frame(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])       # FIN + opcode
        n = len(payload)
        if n < 126:
            header.append(0x80 | n)
        elif n < 65536:
            header.append(0x80 | 126)
            header += struct.pack("!H", n)
        else:
            header.append(0x80 | 127)
            header += struct.pack("!Q", n)
        mask = os.urandom(4)
        header += mask
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        with self._lock:
            self.sock.sendall(bytes(header) + masked)

    # ---------- 对外 ----------
    def send(self, text: str) -> None:
        self._write_frame(0x1, text.encode("utf-8"))

    def recv(self) -> str:
        """读一条完整文本消息（自动跳过事件/处理 ping、拼接分片）。"""
        data = b""
        while True:
            opcode, fin, payload = self._read_frame()
            if opcode == 0x8:                      # close
                raise CDPError("浏览器关闭了调试连接")
            if opcode == 0x9:                      # ping -> pong
                self._write_frame(0xA, payload)
                continue
            if opcode == 0xA:                      # pong
                continue
            if opcode in (0x1, 0x0):               # text / continuation
                data += payload
                if fin:
                    return data.decode("utf-8", "replace")

    def call(self, method: str, params: dict | None = None, msg_id: int = 0) -> dict:
        """发一条 CDP 命令并等它的响应（事件消息直接跳过）。"""
        rid = msg_id or int.from_bytes(os.urandom(3), "big") or 1
        self.send(json.dumps({"id": rid, "method": method, "params": params or {}}))
        while True:
            msg = json.loads(self.recv())
            if msg.get("id") == rid:
                if "error" in msg:
                    raise CDPError(f"{method} 失败：{msg['error']}")
                return msg.get("result") or {}
            # 没有 id = 浏览器推的事件，与我们无关，继续等

    def close(self) -> None:
        try:
            self._write_frame(0x8, b"")
        except Exception:
            pass
        try:
            self.sock.close()
        except Exception:
            pass
