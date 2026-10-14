#!/usr/bin/env python3
"""开发辅助：启动界面服务并用无头 Edge 截图，用于确认视觉效果。

    python scripts/screenshot.py [输出文件名] [宽] [高] [选项]

选项：
    --tab=activity      启动后切到「活跃度」视图
    --tab=history       启动后切到「历史记录」视图
    --draw=3            截图前先执行一次抽奖（抽 3 人），用于查看中奖界面
    --stats             截图前先统计活跃度
    --theme=dark        切换到深色主题

需要本机安装 Edge 或 Chrome。日常使用请直接双击 启动界面.bat。
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import signal
import sys
import tempfile
import threading
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app import find_browser  # noqa: E402
from qq_lottery.service import LotteryService  # noqa: E402
from qq_lottery.settings import ConfigFile  # noqa: E402
from qq_lottery.webapp import create_server  # noqa: E402


def api(port: int, token: str, path: str, method: str = "GET", body: dict | None = None) -> dict:
    url = f"http://127.0.0.1:{port}/api/{path}"
    data = json.dumps(body or {}).encode("utf-8") if method == "POST" else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("X-Lottery-Token", token)
    req.add_header("Content-Type", "application/json")
    with urllib.request.urlopen(req, timeout=60) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _kill_tree(pid: int) -> None:
    """结束浏览器及其所有子进程。

    Chromium 会派生一堆子进程；只杀主进程会留下僵尸，
    它们还锁着 user-data-dir，导致 profile 删不掉。
    """
    if os.name == "nt":
        subprocess.run(
            ["taskkill", "/F", "/T", "/PID", str(pid)],
            capture_output=True, check=False,
        )
        return
    try:
        os.killpg(os.getpgid(pid), signal.SIGKILL)
    except (OSError, AttributeError):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass


def _rmtree_retry(path: Path) -> bool:
    """删目录；返回 True 表示"还没删干净，值得再试一次"。"""
    try:
        shutil.rmtree(path)
        return False
    except FileNotFoundError:
        return False
    except OSError:
        return path.exists()


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    flags = [a for a in sys.argv[1:] if a.startswith("--")]
    out_name = args[0] if args else "ui-shot.png"
    width = int(args[1]) if len(args) > 1 else 1440
    height = int(args[2]) if len(args) > 2 else 1000

    opts = {"tab": "result", "draw": 0, "stats": False, "theme": "", "modal": ""}
    for flag in flags:
        if flag.startswith("--tab="):
            opts["tab"] = flag.split("=", 1)[1]
        elif flag.startswith("--draw="):
            opts["draw"] = int(flag.split("=", 1)[1])
        elif flag == "--stats":
            opts["stats"] = True
        elif flag.startswith("--theme="):
            opts["theme"] = flag.split("=", 1)[1]
        elif flag.startswith("--modal="):
            opts["modal"] = flag.split("=", 1)[1]

    browser = find_browser()
    if not browser:
        print("找不到 Edge / Chrome，无法截图")
        return 1

    # 截图脚本会刷新数据、可能写配置；为了不碰用户的真实 config.json，
    # 这里复制一份到临时目录，让后端只操作副本。
    tmp_dir = Path(tempfile.mkdtemp(prefix="qq-lottery-shot-"))
    real_config = ROOT / "config.json"
    if real_config.is_file():
        shutil.copy2(real_config, tmp_dir / "config.json")
    service = LotteryService(ConfigFile(tmp_dir / "config.json"))
    server, token = create_server(service, port=0)
    port = server.server_address[1]
    threading.Thread(target=server.serve_forever, daemon=True).start()
    print(f"服务已启动：http://127.0.0.1:{port}/")

    preload = False
    if opts["stats"] or opts["tab"] == "activity":
        api(port, token, "stats", "POST", {"reload": True})
        preload = True
        print("已统计活跃度")

    out = ROOT / "out"
    out.mkdir(exist_ok=True)
    target = out / out_name
    if target.exists():
        target.unlink()
    # 每次用一个全新的临时 profile：
    #   · 固定目录会被上一次没退干净的 Edge 锁住，导致下次截图静默失败
    #   · 复用同一个目录还会让浏览器留下上次的会话状态
    profile = Path(tempfile.mkdtemp(prefix="qq-lottery-shot-"))

    url = f"http://127.0.0.1:{port}/?token={token}#auto=1"
    if opts["tab"] != "result":
        url += f"&tab={opts['tab']}"
    if opts["draw"]:
        url += f"&draw={opts['draw']}"
    if opts["theme"]:
        url += f"&theme={opts['theme']}"
    if opts["modal"]:
        url += f"&modal={opts['modal']}"
    cmd = [
        browser,
        "--headless=new",
        "--disable-gpu",
        "--no-sandbox",
        "--no-first-run",
        "--no-default-browser-check",
        f"--user-data-dir={profile}",
        f"--window-size={width},{height}",
        "--virtual-time-budget=9000",
        f"--screenshot={target}",
        url,
    ]
    print("正在截图…")
    proc = None
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
        try:
            proc.wait(timeout=150)
        except subprocess.TimeoutExpired:
            print("截图超时，正在结束浏览器…")
            _kill_tree(proc.pid)
            proc.wait(timeout=20)
    except OSError as exc:
        print(f"启动浏览器失败：{exc}")
    finally:
        if proc is not None and proc.poll() is None:
            _kill_tree(proc.pid)
        server.shutdown()
        server.server_close()
        # 等浏览器把 profile 的文件句柄放掉再删，否则会残留一大堆文件
        for _ in range(10):
            if not _rmtree_retry(profile):
                break
            time.sleep(0.5)
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if target.is_file() and target.stat().st_size > 0:
        print(f"已生成 {target}（{target.stat().st_size} 字节）")
        return 0
    print("截图失败")
    return 1


if __name__ == "__main__":
    os.environ.setdefault("PYTHONIOENCODING", "utf-8")
    sys.exit(main())
