"""无界面常驻：只负责持续把机器人消息增量写入本地存档。

为什么需要它
------------
界面里的自动同步依赖「界面开着」——你不打开界面就不攒历史。
这个常驻进程专管一件事：按固定间隔把消息落盘，与界面完全解耦。

设计要点
--------
  · 与界面**共用同一套存档逻辑**（LotteryService.sync_archive），不会出现两份实现
  · 机器人不可达时**安静重试**：不刷屏，只在状态变化时记一行日志
  · 配置改了（间隔/存档路径）下一轮自动生效，不用重启
  · 支持优雅退出：收到 Ctrl+C 或终止信号后写完当前一轮再走
  · 支持单次运行（--once），方便做计划任务或快速验证
"""

from __future__ import annotations

import json
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from . import paths
from .config import ConfigError
from .service import LotteryService
from .settings import ConfigFile


@dataclass
class WatchEvent:
    """一轮同步的结果，便于测试断言与日志输出。"""

    at: float
    ok: bool
    message: str
    inserted: int = 0
    fetched: int = 0
    total: int = 0
    span_label: str = ""

    def line(self) -> str:
        stamp = datetime.fromtimestamp(self.at).strftime("%Y-%m-%d %H:%M:%S")
        mark = "OK " if self.ok else "ERR"
        return f"[{stamp}] {mark} {self.message}"


class WatchLogger:
    """极简日志：同时写文件和（可选）打印到控制台。"""

    def __init__(self, path: Path | None, echo: bool = True, keep_lines: int = 4000) -> None:
        self.path = path
        self.echo = echo
        self.keep_lines = keep_lines
        if path is not None:
            path.parent.mkdir(parents=True, exist_ok=True)
        self._last_state: str | None = None

    def write(self, text: str, *, only_on_change: str | None = None) -> None:
        if only_on_change is not None:
            # 同样的状态不重复刷屏（例如连续多次"连不上机器人"）
            if self._last_state == only_on_change:
                return
            self._last_state = only_on_change
        elif only_on_change is None and self._last_state is not None:
            self._last_state = None

        if self.echo:
            print(text, flush=True)
        if self.path is None:
            return
        try:
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(text + "\n")
            self._trim()
        except OSError:
            pass

    def _trim(self) -> None:
        """日志别无限长大：超过上限就保留末尾若干行。"""
        if self.path is None or not self.path.is_file():
            return
        try:
            if self.path.stat().st_size < 2 * 1024 * 1024:
                return
            lines = self.path.read_text(encoding="utf-8", errors="replace").splitlines()
            if len(lines) > self.keep_lines:
                self.path.write_text("\n".join(lines[-self.keep_lines :]) + "\n", encoding="utf-8")
        except OSError:
            pass


def run_once(service: LotteryService, logger: WatchLogger | None = None) -> WatchEvent:
    """执行一轮同步，永不抛异常（失败也要变成事件返回）。"""
    now = time.time()
    try:
        result = service.sync_archive(silent=True)
    except Exception as exc:  # noqa: BLE001 常驻进程绝不能因为一次异常退出
        event = WatchEvent(at=now, ok=False, message=f"同步异常：{exc}")
        if logger:
            logger.write(event.line(), only_on_change=f"err:{exc}")
        return event

    if not result.ok:
        event = WatchEvent(at=now, ok=False, message=result.message or result.code)
        if logger:
            logger.write(event.line(), only_on_change=f"err:{result.code}")
        return event

    sync = result.data.get("sync", {})
    archive = result.data.get("archive", {})
    event = WatchEvent(
        at=now,
        ok=True,
        message=(
            f"新增 {sync.get('inserted', 0)} 条 / 更新 {sync.get('updated', 0)} 条"
            f"（库内 {archive.get('total', 0)} 条，跨度 {archive.get('span_label', '—')}）"
        ),
        inserted=int(sync.get("inserted", 0)),
        fetched=int(sync.get("fetched", 0)),
        total=int(archive.get("total", 0)),
        span_label=str(archive.get("span_label", "")),
    )
    if logger:
        logger.write(event.line())
    return event


@dataclass
class WatchResult:
    """常驻运行结束后的汇总，供测试与 CLI 汇报。"""

    rounds: int = 0
    ok_rounds: int = 0
    inserted: int = 0
    events: list[WatchEvent] = field(default_factory=list)
    stopped_by: str = ""

    def summary(self) -> str:
        return (
            f"共 {self.rounds} 轮，成功 {self.ok_rounds} 轮，"
            f"累计新增 {self.inserted} 条消息"
        )


def run_forever(
    service: LotteryService,
    logger: WatchLogger,
    *,
    interval: float,
    force_interval: bool = False,
    max_rounds: int | None = None,
    stop_event: threading.Event | None = None,
    on_round: Callable[[WatchEvent], None] | None = None,
) -> WatchResult:
    """常驻循环：每隔 interval 秒同步一轮。

    · force_interval=True 时忽略配置里的间隔，只用传入值（命令行 --interval 用）
    · max_rounds 用来自检里限定跑几轮
    · stop_event 用外部事件提前停止（CLI 里由信号触发）
    """
    result = WatchResult()
    stop = stop_event or threading.Event()

    while not stop.is_set():
        # 每轮重新读配置：改了存档路径/开关能立刻生效。
        # 但间隔只在没有显式指定时采用配置值，否则命令行的 --interval 会被覆盖。
        wait_seconds = max(5.0, float(interval))
        if not force_interval:
            try:
                cfg = service.config_file.to_config()
                wait_seconds = max(5.0, float(cfg.archive_interval))
            except ConfigError:
                pass

        event = run_once(service, logger)
        result.rounds += 1
        result.inserted += event.inserted
        if event.ok:
            result.ok_rounds += 1
        result.events.append(event)
        if on_round:
            on_round(event)

        if max_rounds is not None and result.rounds >= max_rounds:
            result.stopped_by = "达到轮数上限"
            break
        if stop.is_set():
            result.stopped_by = "收到停止信号"
            break
        # 可中断的等待
        stop.wait(wait_seconds)

    if not result.stopped_by:
        result.stopped_by = "正常结束"
    return result


def install_signal_handlers(stop_event: threading.Event, logger: WatchLogger | None = None) -> None:
    def handler(signum, _frame):  # noqa: ANN001
        if logger:
            logger.write(f"收到信号 {signum}，正在停止…")
        stop_event.set()

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError):
            continue


def build_service(config_path: str | Path | None = None) -> LotteryService:
    config_file = ConfigFile(config_path) if config_path else ConfigFile()
    return LotteryService(config_file)


def default_log_path() -> Path:
    return paths.resolve("data/archive-watch.log")
