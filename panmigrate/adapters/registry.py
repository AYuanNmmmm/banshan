"""网盘提供商注册表。

设计要点：引擎和界面都不硬编码任何一家网盘。新增一家网盘 = 在 adapters/ 下
加一个实现 SourceAdapter / SinkAdapter 的文件，然后在这里登记一行。

roles 表示这家网盘在工具里能扮演什么角色：
  source = 能读（作为迁移的起点）
  sink   = 能写（作为迁移的终点）
两个方向都支持，就意味着任意两家之间都能互相迁，而不是只能 A->B。

status:
  ready    = 已实现，可直接用
  partial  = 已实现部分方向（看 roles）
  planned  = 只有接入方案，代码未写

chunk 描述这家的分片与并发能力，界面据此把「档位」落到这家网盘的真实约束上：
  upload_mb     目标端固定上传分片大小；0 表示可变（由设置决定）
  upload_fixed  是否固定（固定时，设置里的分片大小对它无效）
  read_max_mb   源端单次 Range 读取的硬上限（0 = 不限）
  conn          该网盘作为源端时允许的并发下载连接数上限（0 = 不限）
                引擎会按 min(档位设置, conn) 收敛，避免用户把档位调高后
                被网盘掐连接（百度实测 4 条最优，6 条反而更慢）
实测记录（2026-10-25）：
  百度 下载 dlink 单次 Range 超过 4MB 直接 403 + error_code 31326，
       与登录态/权限无关，纯粹是「一次要太多」。
"""
from __future__ import annotations

PROVIDERS: dict[str, dict] = {
    "baidu": {
        "id": "baidu",
        "name": "百度网盘",
        "roles": ["source", "sink"],
        "auth_modes": ["qr", "cookie", "oauth"],
        "status": "ready",
        "doc": "https://pan.baidu.com/union",
        "note": (
            "推荐扫码登录，扫一下就能读写整个网盘。"
            "上传固定 4MB 分片；不支持 0 字节文件。"
            "下载限速严格：非会员约 100KB/s（建议「稳妥」档），"
            "会员可达 8MB/s（建议「极速」档）"
        ),
        # 下载单次 Range 上限 4MB（超了 403 31326）；上传分片也固定 4MB
        # 能提供哪些整文件摘要（目标端可能要 SHA1，给不出就得补算）
        "digests": ["md5"],
        "chunk": {"upload_mb": 4, "upload_fixed": True, "read_max_mb": 4, "conn": 4},
    },
    "aliyun": {
        "id": "aliyun",
        "name": "阿里云盘",
        "roles": ["sink", "source"],
        "auth_modes": ["qr", "web_token", "oauth"],
        "status": "ready",
        "doc": "https://open.aliyundrive.com",
        "note": "推荐扫码登录。走官方 OAuth 需先提交应用审核、申请 file:all 权限",
        # 分片大小可变：设置里填多少就用多少
        "digests": ["sha1", "md5"],
        "chunk": {"upload_mb": 0, "upload_fixed": False, "read_max_mb": 4, "conn": 4},
    },
    "quark": {
        "id": "quark",
        "name": "夸克网盘",
        "roles": ["source", "sink"],
        "auth_modes": ["qr", "cookie"],
        "status": "ready",
        "doc": "https://pan.quark.cn",
        "note": (
            "推荐扫码登录，扫一下就能读写整个网盘。"
            "上传固定 4MB 分片；不支持 0 字节文件"
        ),
        "digests": ["md5"],
        "chunk": {"upload_mb": 4, "upload_fixed": True, "read_max_mb": 4, "conn": 4},
    },
    "tianyi": {
        "id": "tianyi",
        "name": "天翼云盘",
        "roles": ["source", "sink"],
        "auth_modes": ["cookie"],
        "status": "ready",
        "doc": "https://cloud.189.cn",
        "note": (
            "点「一键登录」弹出浏览器窗口，登录后自动抓取登录态。"
            "上传固定 10MB 分片；不支持 0 字节文件"
        ),
        "digests": [],
        "chunk": {"upload_mb": 10, "upload_fixed": True, "read_max_mb": 4, "conn": 4},
    },
    "pan123": {
        "id": "pan123",
        "name": "123 云盘",
        "roles": ["source", "sink"],
        "auth_modes": ["web_qr"],
        "status": "ready",
        "doc": "https://www.123pan.com",
        "note": (
            "点「扫码登录」弹出浏览器窗口，扫码后自动抓取登录态，不用填任何密钥、"
            "也不用申请开放平台。上传走 S3 分片（16MB 一片，服务端决定）"
        ),
        "digests": ["md5"],
        "chunk": {"upload_mb": 0, "upload_fixed": False, "read_max_mb": 4, "conn": 4},
    },
    "pan115": {
        "id": "pan115",
        "name": "115 网盘",
        "roles": ["source", "sink"],
        "auth_modes": ["web_qr", "cookie"],
        "status": "ready",
        "doc": "https://115.com",
        "note": (
            "点「扫码登录」弹出浏览器窗口，扫码后自动抓取 Cookie；也可以手动粘贴。"
            "115 是整文件上传（不分片），单个文件上限 500MB；超过会明确提示"
        ),
        # 整文件上传，没有分片概念
        "digests": ["sha1"],
        # 目标端特性：预上传要 SHA1；整文件上传，单文件 500MB 上限
        "needs_sha1": True,
        "max_file_mb": 500,
        "chunk": {"upload_mb": 0, "upload_fixed": False, "read_max_mb": 4, "conn": 4},
    },
    "mock": {
        "id": "mock",
        "name": "本地演示（无需账号）",
        "roles": ["source", "sink"],
        "auth_modes": ["none"],
        "status": "ready",
        "doc": "",
        "note": "用本机两个文件夹模拟两端网盘，用来验证引擎、断点续传和校验逻辑。",
        "chunk": {"upload_mb": 0, "upload_fixed": False, "read_max_mb": 0, "conn": 0},
        # 不在界面上出现：它是给自动化测试用的装置，普通用户选了会困惑
        # （看着像「能迁」，其实只是在本机两个文件夹之间拷来拷去）。
        "hidden": True,
    },
}


def list_providers(include_hidden: bool = True) -> list[dict]:
    """全部登记项。include_hidden=False 时过滤掉界面不该出现的（如本地演示）。"""
    items = list(PROVIDERS.values())
    if include_hidden:
        return items
    return [p for p in items if not p.get("hidden")]


def get(provider_id: str) -> dict:
    return PROVIDERS.get(provider_id) or PROVIDERS["mock"]


def supports(provider_id: str, role: str) -> bool:
    return role in get(provider_id).get("roles", [])
