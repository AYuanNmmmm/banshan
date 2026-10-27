"""轻量定时调度：标准 5 字段 cron，够用就好（分 时 日 月 周）。"""
from __future__ import annotations

import threading
import time


def _match_field(expr: str, value: int) -> bool:
    for part in expr.split(","):
        part = part.strip()
        if not part:
            continue
        if part == "*":
            return True
        if part.startswith("*/"):
            try:
                step = int(part[2:])
            except ValueError:
                continue
            if step > 0 and value % step == 0:
                return True
            continue
        if "-" in part:
            left, _, right = part.partition("-")
            try:
                if int(left) <= value <= int(right):
                    return True
            except ValueError:
                continue
            continue
        try:
            if int(part) == value:
                return True
        except ValueError:
            continue
    return False


def cron_matches(expr: str, now: time.struct_time | None = None) -> bool:
    fields = (expr or "").split()
    if len(fields) != 5:
        return False
    now = now or time.localtime()
    minute, hour, dom, month, dow = fields
    weekday = (now.tm_wday + 1) % 7  # cron 里 0=周日
    return (
        _match_field(minute, now.tm_min)
        and _match_field(hour, now.tm_hour)
        and _match_field(dom, now.tm_mday)
        and _match_field(month, now.tm_mon)
        and (_match_field(dow, weekday) or _match_field(dow, 7 if weekday == 0 else weekday))
    )


def describe_cron(expr: str) -> str:
    fields = (expr or "").split()
    if len(fields) != 5:
        return "表达式格式不对，需要 5 个字段，例如 0 3 * * *"
    minute, hour, dom, month, dow = fields
    if minute.isdigit() and hour.isdigit() and dom == "*" and month == "*" and dow == "*":
        return f"每天 {int(hour):02d}:{int(minute):02d}"
    if minute.isdigit() and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"每小时第 {int(minute)} 分钟"
    if minute.startswith("*/") and hour == "*" and dom == "*" and month == "*" and dow == "*":
        return f"每 {minute[2:]} 分钟"
    return f"按表达式 {expr} 执行"


class CronScheduler:
    def __init__(self, job):
        self.job = job
        self.enabled = False
        self.expr = "0 3 * * *"
        self.last_run_key = ""
        self.last_run_at = ""
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()

    def configure(self, enabled: bool, expr: str) -> None:
        self.enabled = bool(enabled)
        self.expr = (expr or "0 3 * * *").strip()

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, name="cron", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                if self.enabled and cron_matches(self.expr):
                    key = time.strftime("%Y%m%d%H%M")
                    if key != self.last_run_key:
                        self.last_run_key = key
                        self.last_run_at = time.strftime("%Y-%m-%d %H:%M:%S")
                        self.job()
            except Exception:
                pass
            if self._stop.wait(20):
                return

    def status(self) -> dict:
        return {
            "enabled": self.enabled,
            "cron": self.expr,
            "human": describe_cron(self.expr),
            "last_run_at": self.last_run_at,
        }
