"""适配器工厂：按 provider id + 认证方式 造出 Source / Sink 实例。"""
from __future__ import annotations

from .base import AdapterError, RemoteItem, SinkAdapter, SourceAdapter, UploadSession
from .registry import PROVIDERS, get, list_providers, supports

__all__ = [
    "AdapterError",
    "RemoteItem",
    "SinkAdapter",
    "SourceAdapter",
    "UploadSession",
    "PROVIDERS",
    "get",
    "list_providers",
    "supports",
    "build_source",
    "build_sink",
]


def build_source(cfg: dict) -> SourceAdapter:
    provider = (cfg or {}).get("provider") or "mock"
    if not supports(provider, "source"):
        raise AdapterError(f"{get(provider)['name']} 暂不支持作为迁移源")

    if provider == "mock":
        from .mock import MockSource

        return MockSource(cfg)

    if provider == "baidu":
        from .baidu import BaiduSource

        return BaiduSource(cfg)

    if provider == "aliyun":
        from .aliyun import AliyunSource

        return AliyunSource(cfg)

    if provider == "tianyi":
        from .tianyi import TianyiSource

        return TianyiSource(cfg)

    if provider == "quark":
        from .quark import QuarkSource

        return QuarkSource(cfg)

    if provider == "pan123":
        from .pan123 import Pan123Source

        return Pan123Source(cfg)

    if provider == "pan115":
        from .pan115 import Pan115Source

        return Pan115Source(cfg)

    raise AdapterError(
        f"{get(provider)['name']} 的适配器还没实现（当前状态：{get(provider)['status']}）"
    )


def build_sink(cfg: dict) -> SinkAdapter:
    provider = (cfg or {}).get("provider") or "mock"
    if not supports(provider, "sink"):
        raise AdapterError(f"{get(provider)['name']} 暂不支持作为迁移目标")

    if provider == "mock":
        from .mock import MockSink

        return MockSink(cfg)

    if provider == "aliyun":
        from .aliyun import AliyunSink

        return AliyunSink(cfg)

    if provider == "baidu":
        from .baidu import BaiduSink

        return BaiduSink(cfg)

    if provider == "tianyi":
        from .tianyi import TianyiSink

        return TianyiSink(cfg)

    if provider == "quark":
        from .quark import QuarkSink

        return QuarkSink(cfg)

    if provider == "pan123":
        from .pan123 import Pan123Sink

        return Pan123Sink(cfg)

    if provider == "pan115":
        from .pan115 import Pan115Sink

        return Pan115Sink(cfg)

    raise AdapterError(
        f"{get(provider)['name']} 的适配器还没实现（当前状态：{get(provider)['status']}）"
    )
