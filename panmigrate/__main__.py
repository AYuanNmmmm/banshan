"""入口：python -m panmigrate"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="panmigrate", description="网盘跨云迁移工具（本地网页版）"
    )
    parser.add_argument("--host", default="127.0.0.1", help="监听地址，默认只监听本机")
    parser.add_argument("--port", type=int, default=8899, help="端口，默认 8899")
    args = parser.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        try:
            sys.stdout.reconfigure(encoding="utf-8")
            sys.stderr.reconfigure(encoding="utf-8")
        except Exception:
            pass

    from .server import serve

    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
