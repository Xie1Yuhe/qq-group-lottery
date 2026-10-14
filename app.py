#!/usr/bin/env python3
"""QQ 群抽奖 · 图形界面启动器。

双击 `启动界面.bat`、或运行 `python app.py` 即可：
  1. 在 127.0.0.1 上启动本地服务（不对外开放）
  2. 用 Edge 的「应用窗口」模式打开界面，看起来就是一个独立的桌面程序
  3. 找不到 Edge 时自动退回默认浏览器

命令行参数：
  --port N        指定端口（默认 8791，被占用会自动换）
  --browser PATH  指定浏览器可执行文件
  --tab           用普通浏览器标签页打开，而不是独立窗口
  --no-open       只启动服务，不打开窗口（便于调试或远程访问）
  --quiet         减少输出
"""

from __future__ import annotations

import argparse
import os
import shutil
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from qq_lottery import paths  # noqa: E402
from qq_lottery.cli import _setup_console  # noqa: E402
from qq_lottery.service import LotteryService  # noqa: E402
from qq_lottery.webapp import create_server  # noqa: E402

BANNER = r"""
   ___  ___  _     _         _   _
  / _ \/ _ \| |   | | ___   | |_| |_  ___
 | (_) | (_) | |__ | |/ _ \  |  _| ' \/ -_)
  \__\_\\__\_\____||_|\___/   \__|_||_\___|
"""

EDGE_CANDIDATES = (
    r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
)
CHROME_CANDIDATES = (
    r"C:\Program Files\Google\Chrome\Application\chrome.exe",
    r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    os.path.join(os.environ.get("LOCALAPPDATA", ""), r"Google\Chrome\Application\chrome.exe"),
)


def find_browser(explicit: str | None = None) -> str | None:
    if explicit:
        return explicit if Path(explicit).is_file() else None
    for name in ("msedge", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    for candidate in EDGE_CANDIDATES + CHROME_CANDIDATES:
        if candidate and Path(candidate).is_file():
            return candidate
    return None


def open_window(url: str, browser: str | None, as_tab: bool = False) -> str:
    """打开界面，返回描述用的字符串。"""
    if as_tab or not browser:
        webbrowser.open(url)
        return "默认浏览器"
    profile_dir = paths.project_root() / ".browser-profile"
    profile_dir.mkdir(exist_ok=True)
    args = [
        browser,
        f"--app={url}",
        f"--user-data-dir={profile_dir}",
        "--no-first-run",
        "--no-default-browser-check",
        "--window-size=1280,880",
    ]
    try:
        subprocess.Popen(args, close_fds=True)
    except OSError:
        webbrowser.open(url)
        return "默认浏览器"
    return Path(browser).name + " 独立窗口"


def set_console_title(title: str) -> None:
    """给控制台窗口设置中文标题（避免把中文写进 .bat 被代码页破坏）。"""
    if os.name != "nt":
        return
    try:
        import ctypes

        ctypes.windll.kernel32.SetConsoleTitleW(title)
    except Exception:
        pass


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="QQ 群抽奖图形界面")
    parser.add_argument("--port", type=int, default=8791, help="本地服务端口（默认 8791）")
    parser.add_argument("--browser", help="指定浏览器可执行文件")
    parser.add_argument("--tab", action="store_true", help="用普通浏览器标签页打开")
    parser.add_argument("--no-open", action="store_true", help="只启动服务，不打开窗口")
    parser.add_argument("--clean-profile", action="store_true", help="退出时清理浏览器临时配置目录")
    parser.add_argument("--quiet", action="store_true", help="减少输出")
    args = parser.parse_args(argv)

    _setup_console()
    set_console_title("QQ 群抽奖 · 图形界面")

    def say(text: str = "") -> None:
        if not args.quiet:
            print(text, flush=True)

    say(BANNER)
    say("  QQ 群活跃群友抽奖 · 图形界面")
    say("  " + "─" * 46)
    say(f"  程序目录：{paths.project_root()}")

    service = LotteryService()
    try:
        server, _token = create_server(service, port=args.port)
    except OSError as exc:
        print(f"  ✗ 无法启动本地服务：{exc}", file=sys.stderr)
        return 1

    url = f"http://127.0.0.1:{server.server_address[1]}/?token={server.token}"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    say(f"  本地地址：http://127.0.0.1:{server.server_address[1]}/")
    say("  " + "─" * 46)

    if args.no_open:
        say("  已启动服务（--no-open，未打开窗口）。按 Ctrl+C 退出。")
    else:
        browser = find_browser(args.browser)
        target = open_window(url, browser, as_tab=args.tab)
        if browser and not args.tab:
            say(f"  界面已在 {target} 中打开")
            say("  关闭那个窗口即可结束程序；也可以在这里按 Ctrl+C。")
        else:
            say(f"  界面已在 {target} 中打开（未找到 Edge/Chrome，已退回默认浏览器）")
            say("  按 Ctrl+C 退出程序。")
    say("")

    try:
        while True:
            time.sleep(0.6)
    except KeyboardInterrupt:
        say("\n  正在退出…")
    finally:
        server.shutdown()
        server.server_close()
        # 关闭存档连接，确保 WAL 内容落盘（否则最近的同步可能还在 -wal 里）
        try:
            service.close_archives()
        except Exception:  # noqa: BLE001
            pass
        if args.clean_profile:
            shutil.rmtree(paths.project_root() / ".browser-profile", ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
