#!/usr/bin/env python3
"""把海报 HTML 逐页截成 PNG（1080×1440，2x）。

用法：python3 shot.py <poster.html> <输出目录> [页数]
输出：<输出目录>/poster-1.png … poster-N.png（默认 4 页）
"""
from __future__ import annotations

import base64
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, "/Users/yuan/WorkBuddy/2026-10-27-08-04-22/搬山-v1.26.0")
from panmigrate import cdp  # noqa: E402

CHROME = "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"
PORT = 9340
W, H = 1080, 1440


def main() -> int:
    html = Path(sys.argv[1]).resolve()
    outdir = Path(sys.argv[2]).resolve()
    pages = int(sys.argv[3]) if len(sys.argv) > 3 else 4
    outdir.mkdir(parents=True, exist_ok=True)

    proc = subprocess.Popen([
        CHROME, "--headless=new", "--disable-gpu", "--no-sandbox", "--hide-scrollbars",
        f"--remote-debugging-port={PORT}", "--remote-allow-origins=*",
        f"--window-size={W},{H}", "--user-data-dir=/tmp/chrome-prof-poster",
        "about:blank",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        targets = []
        for _ in range(60):
            try:
                targets = [t for t in cdp.http_json(PORT, "/json/list", timeout=2)
                           if t.get("type") == "page"]
                if targets:
                    break
            except Exception:
                time.sleep(0.4)
        ws = cdp.WebSocket(targets[0]["webSocketDebuggerUrl"], timeout=30)
        ws.call("Page.enable")
        ws.call("Emulation.setDeviceMetricsOverride",
                {"width": W, "height": H, "deviceScaleFactor": 2, "mobile": False})
        for n in range(1, pages + 1):
            ws.call("Page.navigate", {"url": f"file://{html}?p={n}"})
            time.sleep(1.6)
            shot = ws.call("Page.captureScreenshot", {"format": "png"})
            f = outdir / f"poster-{n}.png"
            f.write_bytes(base64.b64decode(shot["data"]))
            print(f"已保存 {f}")
        ws.close()
        return 0
    finally:
        proc.terminate()


if __name__ == "__main__":
    raise SystemExit(main())
