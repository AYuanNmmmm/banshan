"""打包入口：PyInstaller 打包时用它作为启动脚本。

普通开发时用 `python -m panmigrate`（走 __main__.py 的相对导入），
打包时用这个文件（绝对导入），避免 PyInstaller 直接执行 __main__.py 时
相对导入失败（attempted relative import with no known parent package）。
"""
from __future__ import annotations

import argparse
import sys


def main() -> int:
    parser = argparse.ArgumentParser(
        prog="搬山", description="网盘跨云迁移工具（本地网页版）"
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

    from panmigrate.server import serve

    serve(args.host, args.port)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
