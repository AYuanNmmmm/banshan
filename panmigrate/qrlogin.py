"""扫码登录：用户不用填任何东西，手机上扫一下就把凭据拿到。

两家都是走「网页端登录」流程获取登录态，而不是官方开放平台的应用授权。
差别要讲清楚：

  官方 OAuth   → 需要先在开放平台注册应用，拿到 client_id/secret，用户填完再授权。
                 合规，但门槛高，而且百度那边只能读写 /apps 目录。
  扫码登录     → 直接模拟官方网页端的登录二维码。零填写，能读全盘。
                 代价是拿到的是网页登录态，会过期、需要续期，且平台可能随时改。

所以界面默认给扫码，官方 OAuth 收进「高级」里，两条路都留着。

接口都是实测过的（能正常返回二维码、轮询返回等待状态），最后一跳"确认授权"
需要真人扫码才能验证。
"""
from __future__ import annotations

import base64
import gzip
import io
import json
import sys
import zlib
import time
import uuid
import urllib.parse
from pathlib import Path

from .adapters import httpc


def _vendor_dir() -> Path:
    """定位 vendor 目录（含纯 Python 的 qrcode 库）。

    开发环境：项目根/vendor。
    打包环境（PyInstaller）：vendor 被一起打进去，落在 sys._MEIPASS/vendor。
    """
    meipass = getattr(sys, "_MEIPASS", None)
    if meipass:
        p = Path(meipass) / "vendor"
        if p.is_dir():
            return p
    return Path(__file__).resolve().parent.parent / "vendor"


VENDOR = _vendor_dir()
if VENDOR.is_dir() and str(VENDOR) not in sys.path:
    sys.path.insert(0, str(VENDOR))


class QRError(Exception):
    pass


def _ms() -> int:
    return int(time.time() * 1000)


def _b64(data: bytes, mime: str) -> str:
    return f"data:{mime};base64," + base64.b64encode(data).decode("ascii")


def _render_qr_svg(content: str) -> str | None:
    """把任意文本渲染成二维码（内联 SVG data URI），不依赖 Pillow。"""
    try:
        import qrcode
        import qrcode.image.svg

        buf = io.BytesIO()
        img = qrcode.make(
            content,
            image_factory=qrcode.image.svg.SvgPathImage,
            box_size=10,
            border=2,
        )
        img.save(buf)
        return _b64(buf.getvalue(), "image/svg+xml")
    except Exception:
        return None



def _decode_bizext(raw: str) -> dict:
    """阿里云盘的 bizExt 是 base64 包的一坨东西。

    实测不总是 UTF-8 的明文 JSON —— 可能带压缩、也可能是 GBK
    （用户昵称里有中文时尤其如此）。所以逐层试，别一上来就当成 UTF-8。
    """
    blob = base64.b64decode(
        raw.replace("-", "+").replace("_", "/") + "=" * (-len(raw) % 4)
    )
    variants = [blob]
    for decompress in (gzip.decompress, zlib.decompress):
        try:
            variants.append(decompress(blob))
        except Exception:
            pass
    for candidate in variants:
        for encoding in ("utf-8", "gbk", "utf-8-sig"):
            try:
                obj = json.loads(candidate.decode(encoding))
            except Exception:
                continue
            if isinstance(obj, dict):
                return obj
    raise ValueError("bizExt 解析不出 JSON")


def _find_token(obj, keys: set) -> str:
    """在嵌套 JSON 里递归找第一个匹配的字符串字段。"""
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in keys and isinstance(value, str) and value.strip():
                return value.strip()
        for value in obj.values():
            found = _find_token(value, keys)
            if found:
                return found
    elif isinstance(obj, list):
        for value in obj:
            found = _find_token(value, keys)
            if found:
                return found
    return ""


class BaseQRLogin:
    provider = ""
    label = ""
    ttl_seconds = 300

    def __init__(self, role: str):
        self.role = role
        self.sid = uuid.uuid4().hex
        self.created_at = time.time()
        self.done = False
        self.credentials: dict | None = None
        self.last_raw: dict = {}          # 上游返回的关键字段，用于排错
        self.last_status = ""             # 上一次的归一化状态
        self.polls = 0

    def start(self) -> dict:
        raise NotImplementedError

    def poll(self) -> dict:
        raise NotImplementedError

    def _payload(self, image: str | None, hint: str, fallback: str = "") -> dict:
        return {
            "sid": self.sid,
            "provider": self.provider,
            "label": self.label,
            "image": image,
            "hint": hint,
            "fallback": fallback,
            "expires_in": self.ttl_seconds,
        }


# --------------------------------------------------------------------------- #
# 阿里云盘
# --------------------------------------------------------------------------- #
class AliyunQRLogin(BaseQRLogin):
    provider = "aliyun"
    label = "阿里云盘"
    GEN = "https://passport.aliyundrive.com/newlogin/qrcode/generate.do"
    QUERY = "https://passport.aliyundrive.com/newlogin/qrcode/query.do"

    def __init__(self, role: str):
        super().__init__(role)
        self.sess = httpc.Session(
            headers={
                "Accept": "application/json, text/plain",
                "Accept-Language": "zh-CN,zh;q=0.9",
                "Referer": "https://www.aliyundrive.com/",
            }
        )
        self.t = 0
        self.ck = ""
        self.bizext_raw = ""

    def start(self) -> dict:
        query = (
            "?appName=aliyun_drive&fromSite=52&appEntrance=web&isMobile=false"
            "&lang=zh_CN&returnUrl=&bizParams=&_bx-v=2.0.31"
        )
        data = self.sess.get_json(self.GEN + query)
        block = (data.get("content") or {}).get("data") or {}
        content = block.get("codeContent") or ""
        if not content:
            raise QRError(f"没拿到二维码：{block.get('title_msg') or data}")
        self.t = block.get("t") or 0
        self.ck = block.get("ck") or ""
        image = _render_qr_svg(content)
        return self._payload(
            image,
            "打开手机上的阿里云盘 App，扫一扫上面的二维码，然后在手机上点确认",
            content if image is None else "",
        )

    def poll(self) -> dict:
        fields = {
            "t": str(self.t),
            "ck": self.ck,
            "appName": "aliyun_drive",
            "appEntrance": "web",
            "isMobile": "false",
            "lang": "zh_CN",
            "returnUrl": "",
            "fromSite": "52",
            "bizParams": "",
            "navlanguage": "zh-CN",
            "navPlatform": "MacIntel",
        }
        data = self.sess.post_form(
            self.QUERY + "?appName=aliyun_drive&fromSite=52&_bx-v=2.0.31", fields
        )
        block = (data.get("content") or {}).get("data") or {}
        status = (block.get("qrCodeStatus") or "").upper()
        self.last_raw = {
            "qrCodeStatus": status or "(空)",
            "resultCode": block.get("resultCode"),
            "hasBizExt": bool(block.get("bizExt")),
        }

        if status == "NEW":
            return {"status": "waiting", "message": "等待扫码…"}
        if status == "SCANED":
            return {"status": "scanned", "message": "已扫码，请在手机上点「确认登录」"}
        if status == "CONFIRMED":
            refresh = ""
            raw = block.get("bizExt") or ""
            if raw:
                self.bizext_raw = raw
                try:
                    info = _decode_bizext(raw)
                    refresh = _find_token(info, {"refreshToken", "refresh_token"})
                    self.last_raw["bizKeys"] = list(info.keys())[:10]
                except Exception as exc:
                    self.last_raw["bizError"] = f"{type(exc).__name__}: {exc}"
            if not refresh:
                if self.bizext_raw:
                    raise QRError(
                        "扫码已确认，但没能从返回数据里解出 refresh_token。"
                        "我已经把原始数据记下来了，告诉开发者可以继续排查；"
                        "眼下建议改用「认证方式 → 网页 Token」手动粘贴。"
                    )
                raise QRError("扫码已确认，但上游没返回 bizExt，请重试")
            self.done = True
            self.credentials = {"refresh_token": refresh, "auth_mode": "qr"}
            return {"status": "confirmed", "message": "已登录阿里云盘", "done": True}
        if status == "EXPIRED":
            return {"status": "expired", "message": "二维码已过期，请点「换个二维码」"}
        if status == "CANCELED":
            return {"status": "canceled", "message": "已在手机上取消授权"}
        return {"status": "waiting", "message": f"等待扫码…（{status or '未知状态'}）"}


# --------------------------------------------------------------------------- #
# 百度网盘
# --------------------------------------------------------------------------- #
class BaiduQRLogin(BaseQRLogin):
    provider = "baidu"
    label = "百度网盘"
    GETQR = "https://passport.baidu.com/v2/api/getqrcode"
    UNICAST = "https://passport.baidu.com/channel/unicast"
    BDUZZ = "https://passport.baidu.com/v3/login/main/qrbdusslogin"

    def __init__(self, role: str):
        super().__init__(role)
        self.sess = httpc.Session(
            headers={
                "Referer": "https://pan.baidu.com/",
                "Accept-Language": "zh-CN,zh;q=0.9",
            }
        )
        self.sign = ""
        self.temp_bduss = ""
        self.last_login_try = 0.0

    def start(self) -> dict:
        stamp = _ms()
        url = (
            f"{self.GETQR}?lp=pc&qrloginfrom=pc&callback=&tpl=netdisk"
            f"&apiver=v3&tt={stamp}&_={stamp}"
        )
        data = self.sess.get_json(url)
        if int(data.get("errno") or 0) != 0:
            raise QRError(f"百度没给出二维码：{data.get('prompt') or data}")
        self.sign = data.get("sign") or ""
        if not self.sign:
            raise QRError(f"百度返回里没有 sign：{data}")
        imgurl = data.get("imgurl") or ""
        if imgurl.startswith("/"):
            imgurl = "https://passport.baidu.com" + imgurl
        elif not imgurl.startswith("http"):
            imgurl = "https://" + imgurl
        try:
            raw = self.sess.get_bytes(imgurl)
            image = _b64(raw, "image/png")
        except Exception:
            image = None
        return self._payload(
            image,
            "打开手机上的百度网盘 App，扫一扫上面的二维码，然后在手机上确认登录",
            imgurl if image is None else "",
        )

    def poll(self) -> dict:
        stamp = _ms()
        url = (
            f"{self.UNICAST}?channel_id={self.sign}&tpl=netdisk"
            f"&apiver=v3&tt={stamp}&callback="
        )
        try:
            # 百度这里是真长轮询：没事件时连接会一直挂着（实测 >12s 不返回）。
            # 所以超时要给长、不能重试；用户一扫，挂着的请求立刻就会带着状态返回。
            # 前端用极短的间隔接力，保证任何时刻都有请求在监听，不漏事件。
            text = self.sess.get_text(url, timeout=25, retries=0).strip()
        except (httpc.HttpError, OSError) as exc:
            self.last_raw = {"err": f"{type(exc).__name__}", "body": "(无响应/超时)"}
            return {"status": "waiting", "message": "等待扫码…"}
        if not text:
            self.last_raw = {"err": "(空响应)", "body": ""}
            return {"status": "waiting", "message": "等待扫码…"}
        try:
            outer = json.loads(text)
        except json.JSONDecodeError:
            self.last_raw = {"err": "非 JSON", "body": text[:200]}
            return {"status": "waiting", "message": "等待扫码…"}
        if int(outer.get("errno") or 0) != 0:
            self.last_raw = {"errno": outer.get("errno"), "body": text[:200]}
            return {"status": "waiting", "message": "等待扫码…"}

        channel = outer.get("channel_v") or ""
        try:
            inner = json.loads(channel) if isinstance(channel, str) else (channel or {})
        except json.JSONDecodeError:
            self.last_raw = {"err": "channel_v 解析失败", "body": text[:200]}
            return {"status": "waiting", "message": "等待扫码…"}

        state = int(inner.get("status") or 0)
        temp = (inner.get("v") or "").strip()
        self.last_raw = {"state": state, "hasV": bool(temp), "body": text[:200]}

        if state == 3:
            return {"status": "expired", "message": "二维码已过期，请点「换个二维码」"}

        # 关键：百度在"已扫码"时就会下发 v（临时 bduss），此时 status 仍是 0/1。
        # 所以不能只看 status，要看有没有 v —— 之前只认 status==2，等于永远等不到，
        # 这就是"扫了码没反应"的直接原因。
        if temp:
            self.temp_bduss = temp
        if self.temp_bduss:
            now = time.time()
            if now - self.last_login_try >= 3:
                self.last_login_try = now
                done = self._finish(self.temp_bduss)
                if done:
                    return done
            return {"status": "scanned", "message": "已扫码，请在手机上点确认登录"}

        if state == 1:
            return {"status": "scanned", "message": "已扫码，请在手机上点确认登录"}
        return {"status": "waiting", "message": "等待扫码…"}

    def _finish(self, temp_bduss: str):
        """拿临时凭证换正式 BDUSS。

        用户还没在手机上点确认时，这里拿不到 cookie —— 这种情况返回 None，
        让轮询继续等，而不是抛错吓人。
        """
        stamp = _ms()
        url = (
            f"{self.BDUZZ}?v={stamp}&bduss={temp_bduss}"
            "&u=https%3A%2F%2Fpan.baidu.com%2Fmain&tpl=netdisk&apiver=v3"
            f"&tt={stamp}&traceid=&time={stamp}&alg=v3&callback="
        )
        try:
            self.sess.get_text(url, timeout=15, retries=0)
        except (httpc.HttpError, OSError):
            return None

        bduss = self.sess.cookie("BDUSS")
        if not bduss:
            # 还没确认时拿不到 cookie，安静地继续等
            self.last_raw["cookieNames"] = ";".join(c.name for c in self.sess.jar)[:160]
            return None

        # 关键：不能只留 BDUSS/STOKEN。百度风控会看 BAIDUID（浏览器设备标识），
        # 缺了它下载 dlink 会偶发 403 「31326 user is not authorized, hitcode:104」——
        # 表现就是「有时能下、有时报错」。这里把登录后拿到的相关 cookie 都带上。
        wanted = ("BDUSS", "STOKEN", "BAIDUID", "BAIDUID_BFESS", "PANWEB")
        parts = []
        for name in wanted:
            value = self.sess.cookie(name)
            if value:
                parts.append(f"{name}={value}")
        # 兜底：万一以后百度又加了别的必要字段，把 jar 里 pan.baidu.com 域的
        # 非敏感项也一并带上（排除纯统计类），避免再踩同样的坑。
        skip = {"H_PS_PSSID", "BIDUPSID", "PSTM", "ZFY", "BD_HOME", "BD_UPN"}
        seen = set(wanted)
        for c in self.sess.jar:
            if c.name in seen or c.name in skip or not c.value:
                continue
            if "baidu.com" in (c.domain or ""):
                parts.append(f"{c.name}={c.value}")
                seen.add(c.name)

        cookie = "; ".join(parts) if parts else f"BDUSS={bduss}"
        self.done = True
        self.credentials = {"cookie": cookie, "auth_mode": "qr"}
        return {"status": "confirmed", "message": "已登录百度网盘", "done": True}


# --------------------------------------------------------------------------- #
# 夸克网盘
# --------------------------------------------------------------------------- #
class QuarkQRLogin(BaseQRLogin):
    """夸克网盘扫码登录（官方 CAS，client_id=532）。

    流程（实测 2026-10-24，对齐官方 web 端 / quarkpan 的参数）：
      1. GET uop.quark.cn/cas/ajax/getTokenForQrcodeLogin
           ?client_id=532&v=1.2&request_id=<uuid>  -> data.members.token
      2. 二维码 = su.quark.cn/4_eMHBJ?token=...&client_id=532&ssb=weblogin
      3. 轮询 uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken
           ?client_id=532&v=1.2&token=...&request_id=<uuid>
           成功时 status=2000000，ticket 在 data.members.service_ticket
      4. pan.quark.cn/account/info?st=<ticket>&lw=scan 的 Set-Cookie 即登录态
         （含 __pus / __puus 强校验项）

    坑（务必保留）：
      - 前两步都必须带 client_id=532&v=1.2&request_id，漏了服务端可能不认，
        尤其第二步拿 service_ticket 时，缺参数会一直返回 50004001（Query result
        is empty），表现为「扫了码没反应 / 登不上」。
      - ticket 字段是 data.members.service_ticket（带下划线），别硬编码在顶层。
    """

    provider = "quark"
    label = "夸克网盘"
    GET_TOKEN = "https://uop.quark.cn/cas/ajax/getTokenForQrcodeLogin"
    GET_TICKET = "https://uop.quark.cn/cas/ajax/getServiceTicketByQrcodeToken"
    ACCOUNT_INFO = "https://pan.quark.cn/account/info"

    def __init__(self, role: str):
        super().__init__(role)
        self.sess = httpc.Session(headers={
            "Accept": "application/json, text/plain, */*",
            "Accept-Language": "zh-CN,zh;q=0.9",
            "Cache-Control": "no-cache",
            "Pragma": "no-cache",
            "Referer": "https://pan.quark.cn/",
            "Origin": "https://pan.quark.cn",
            "Sec-Fetch-Dest": "empty",
            "Sec-Fetch-Mode": "cors",
            "Sec-Fetch-Site": "same-site",
        })
        self.token = ""
        self._home_visited = False
        # 已经扫码确认拿到的 ticket。换 cookie 失败时靠它重试，避免让用户重扫。
        self.confirmed_ticket = ""
        self._exchange_fails = 0

    def _q(self) -> str:
        """前两步公用的查询参数（client_id + v + request_id）。"""
        return f"client_id=532&v=1.2&request_id={uuid.uuid4().hex}"

    def start(self) -> dict:
        # 先访一次网页首页，把 ctoken 这类基础 cookie 拿到手。
        # 浏览器真实流程是「先打开 pan.quark.cn → 再扫码 → 跳 account/info」，
        # 少了这一步，换登录态时服务端可能不认这个会话（表现就是 Set-Cookie
        # 里只有 ctoken、没有登录态），扫码确认了也「登不上」。
        # 失败不致命，降级继续。
        if not self._home_visited:
            try:
                self.sess.get_text("https://pan.quark.cn/", timeout=20, retries=0)
            except (httpc.HttpError, OSError):
                pass
            self._home_visited = True

        url = f"{self.GET_TOKEN}?{self._q()}"
        try:
            data = self.sess.get_json(url, headers={"Referer": "https://pan.quark.cn/"}, retries=1)
        except (httpc.HttpError, OSError) as exc:
            raise QRError(f"夸克没给出二维码：{exc}") from None
        token = ""
        if isinstance(data, dict):
            # 结构是 data.members.token，也可能是平铺的 token
            token = _find_token(data, {"token", "qrcodeToken", "qrToken"})
        if not token:
            raise QRError(f"夸克返回里没有 token：{data}")
        self.token = token
        # 二维码指向夸克的扫码确认页；同时用本地渲染一份，前端直接展示。
        # 注意：uc_param_str 和 uc_biz_str 两个参数不能省 —— 夸克手机端对
        # 「端内登录确认页」会校验这些业务参数，缺了会当成无效/过期码处理
        # （这就是之前「手机一直提示二维码过期扫不上」的根因之一）。
        content = (
            f"https://su.quark.cn/4_eMHBJ?token={token}&client_id=532&ssb=weblogin"
            f"&uc_param_str=&uc_biz_str=S%3Acustom%7COPT%3ASAREA%400"
            f"%7COPT%3AIMMERSIVE%401%7COPT%3ABACK_BTN_STYLE%400"
        )
        image = _render_qr_svg(content)
        return self._payload(
            image,
            "打开手机上的夸克 App 扫一扫上面的二维码，然后在手机上确认登录",
            content if image is None else "",
        )

    def poll(self) -> dict:
        url = f"{self.GET_TICKET}?{self._q()}&token={self.token}"
        try:
            data = self.sess.get_json(url, retries=1)
        except (httpc.HttpError, OSError):
            self.last_raw = {"err": "(无响应/超时)"}
            return {"status": "waiting", "message": "等待扫码…"}

        if not isinstance(data, dict):
            data = {}
        status = data.get("status")
        message = str(data.get("message") or "")
        self.last_raw = {"status": status, "message": message[:120]}

        # 1) 只有「确认完成」才算数。
        #    夸克确认完成时返回 status=2000000 + message=ok；
        #    如果手机只扫码还没点确认（50004002），响应里也可能带一个 service_ticket，
        #    但那个 ticket 还没生效 —— 拿它去换登录态会被服务端拒（AUTH_ERROR:50051），
        #    表现就是「手机明明点了确认，却提示登录态不完整」。
        #    所以这里必须先看状态码，确认了才换。
        status_ok = str(status) == "2000000" or message.strip().lower() == "ok"
        ticket = _find_token(data, {"serviceTicket", "service_ticket", "serviceTicketData"})

        if ticket and not status_ok and not self.confirmed_ticket:
            # 已扫码、未确认：把 ticket 先记着但**不换**，等下一轮状态变 2000000
            self.last_raw["pendingTicket"] = ticket[:12]
            return {"status": "scanned", "message": "已扫码，请在手机上点「确认登录」"}

        if ticket and status_ok:
            # 记下来：万一换 cookie 这次没成功，下一轮还能继续用，不必让用户重扫
            self.confirmed_ticket = ticket
        if self.confirmed_ticket:
            try:
                cookie = self._exchange(self.confirmed_ticket)
            except QRError as exc:
                # 手机已经确认过了，不能因为换 cookie 失败就把整个会话作废。
                # 自动再试几轮（ticket 有时刚生成、服务端还没生效），
                # 超过次数才真的报失败，并把真实原因带出去。
                self._exchange_fails += 1
                if self._exchange_fails >= 8:
                    return {"status": "expired", "message": str(exc)}
                return {
                    "status": "waiting",
                    "message": (
                        f"手机上已确认，正在换取登录态（第 {self._exchange_fails} 次未成功）"
                        f"：{exc}"
                    ),
                }
            self.done = True
            self.credentials = {"cookie": cookie, "auth_mode": "qr"}
            return {"status": "confirmed", "message": "已登录夸克网盘", "done": True}

        # 2) 已扫码但未确认（看 message 或状态码关键字）
        s = str(status)
        if any(k in message for k in ("等待确认", "已扫码", "scanned", "确认")):
            return {"status": "scanned", "message": "已扫码，请在手机上点「确认登录」"}

        # 3) 过期 / 失效 / 失败：夸克轮询接口的状态码语义
        #    50004001 = 等待扫码（未过期，继续等）
        #    50004002 = 已扫码未确认（也可能出现在这里，稳妥起见归为「等待/已扫码」）
        #    50004003 = 二维码过期
        #    50004004 = 二维码失效/取消
        #    message 里 expired/failed/error/timeout/invalid 也都算失败
        if any(k in message for k in ("过期", "失效", "expired", "failed", "error", "timeout", "invalid")):
            return {"status": "expired", "message": "二维码已过期，请点「换个二维码」"}
        if s in ("50004003", "50004004"):
            return {"status": "expired", "message": "二维码已过期，请点「换个二维码」"}
        if s == "50004002":
            # 已扫码但还没确认，归到「已扫码」提示用户去手机确认
            return {"status": "scanned", "message": "已扫码，请在手机上点「确认登录」"}

        # 4) 其余都当「等待扫码」（未扫码的 50004001 也走这里）
        return {"status": "waiting", "message": "等待扫码…"}

    def _exchange(self, ticket: str) -> str:
        """用 service_ticket 换登录态 cookie。

        关键（别改回去）：**不能靠「cookie 里有没有 __pus/__puus」判断成败**。
        夸克的 cookie 名会变，而 Set-Cookie 也可能只在某些条件下下发，硬校验会把
        「其实已经登上了」误判成失败 —— 用户看到的「cookie 不完整（缺
        __pus/__puus）」就是这么来的。

        改为「以结果为准」：拿到 cookie 后直接实调一次「读网盘」接口，
        能读通就算登录成功。失败时把真实原因（接口 code/msg、拿到的 cookie 名）
        一并报出来，而不是笼统说一句「cookie 不完整」。

        ticket 通常可重复使用直到过期，所以失败会复用同一个 ticket 重试一轮。
        """
        last_reason = ""
        raw = ""
        for attempt in range(2):
            if attempt:
                time.sleep(1.0)
            try:
                _, _, body = self.sess.request(
                    "GET",
                    f"{self.ACCOUNT_INFO}?st={urllib.parse.quote(ticket)}&lw=scan",
                    timeout=20,
                    retries=0,
                )
                raw = body.decode("utf-8", "replace")
            except (httpc.HttpError, OSError) as exc:
                last_reason = f"换取登录态时请求失败：{exc}"
                continue

            # 接口明确回 success:false（如 AUTH_ERROR:50051 Invalid service ticket）
            # 就把真实 code/msg 带出去，这比「cookie 不完整」有用得多
            try:
                info = json.loads(raw)
            except Exception:
                info = {}
            if isinstance(info, dict) and info.get("success") is False:
                code = str(info.get("code") or "").strip()
                msg = str(info.get("msg") or info.get("message") or "").strip()
                last_reason = f"用 ticket 换登录态被拒（{code} {msg}）".strip()
                continue

            cookie = self.sess.cookie_header()
            ok, why = self._verify_cookie(cookie)
            if ok:
                return cookie
            last_reason = why

        names = ";".join(sorted({c.name for c in self.sess.jar})) or "(无)"
        self.last_raw.update(
            {"cookieNames": names[:200], "accountInfo": raw[:300], "reason": last_reason[:200]}
        )
        raise QRError(
            f"{last_reason or '换取登录态失败'}。已拿到的 cookie：[{names[:120]}]。"
            "可以先点「换个二维码」重扫一次；"
            "若一直不行，把面板上方「认证方式」改成「网页 Cookie」，"
            "在浏览器打开 pan.quark.cn 登录后按 F12 → Network 任一请求，"
            "把 Cookie 整条复制过来即可"
        )

    def _verify_cookie(self, cookie: str) -> tuple[bool, str]:
        """用拿到的 cookie 实调一次「列根目录」，验证登录态是否真的可用。

        这是唯一可靠的判定方式：cookie 名字随便怎么变，能用就是能用。
        """
        if not cookie:
            return False, "换取登录态后没有拿到任何 cookie"
        try:
            data = httpc.get_json(
                "https://drive-pc.quark.cn/1/clouddrive/file/sort"
                "?pr=ucpro&fr=pc&uc_param_str=&pdir_fid=0&_page=1&_size=1&_fetch_total=0",
                headers={
                    "Cookie": cookie,
                    "User-Agent": httpc.DEFAULT_UA,
                    "Accept": "application/json, text/plain, */*",
                    "Referer": "https://pan.quark.cn/",
                },
                retries=0,
            )
        except (httpc.HttpError, OSError) as exc:
            return False, f"验证登录态时请求失败：{exc}"
        if isinstance(data, dict) and int(data.get("code") or 0) == 0:
            return True, ""
        code = data.get("code") if isinstance(data, dict) else ""
        msg = (data.get("message") or data.get("msg") or "") if isinstance(data, dict) else ""
        return False, f"登录态校验未通过（code={code} {str(msg)[:80]}）"


def create(provider: str, role: str) -> BaseQRLogin:
    if provider == "aliyun":
        return AliyunQRLogin(role)
    if provider == "baidu":
        return BaiduQRLogin(role)
    if provider == "quark":
        return QuarkQRLogin(role)
    raise QRError(f"{provider} 不支持扫码登录")
