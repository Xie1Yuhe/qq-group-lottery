"""NapCat 管理：定位安装、启停进程、扫码登录、状态查询。

这个模块只负责"把机器人管起来"，不做任何群消息发送。
界面上"登录机器人"的整个流程都走这里。
"""

from __future__ import annotations

import base64
import json
import os
import socket
import subprocess
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable

from . import paths
from .sources import OneBotClient, OneBotError

# 常见安装位置（一键安装版会生成 NapCat.<版本>.Shell 目录）
SEARCH_ROOTS = (
    "D:\\NapCat",
    "E:\\NapCat",
    "C:\\NapCat",
    "D:\\Tools\\NapCat",
    "E:\\Tools\\NapCat",
)
SHELL_MARKERS = ("napcat.bat", "NapCatWinBootMain.exe", "launcher.bat", "package.json")

# 进程列表与安装目录的缓存（避免界面轮询时反复扫盘/起进程）
_PROC_CACHE: list[dict[str, Any]] | None = None
_PROC_CACHE_AT: float = 0.0
_INSTALL_CACHE: "NapCatInstall | None" = None
_INSTALL_CACHE_AT: float = 0.0


@dataclass
class NapCatInstall:
    """一次 NapCat 安装的定位结果。"""

    root: Path
    shell: Path
    version: str = ""
    config_dir: Path | None = None
    launcher: Path | None = None
    autostart: Path | None = None

    def as_dict(self) -> dict[str, Any]:
        return {
            "root": str(self.root),
            "shell": str(self.shell),
            "version": self.version,
            "config_dir": str(self.config_dir) if self.config_dir else "",
            "launcher": str(self.launcher) if self.launcher else "",
            "autostart": str(self.autostart) if self.autostart else "",
        }


def _iter_candidate_roots(hint: str | Path | None = None) -> Iterable[Path]:
    if hint:
        yield Path(hint)
    for base in SEARCH_ROOTS:
        yield Path(base)
    # 各盘符根目录下形如 NapCat* 的目录
    for drive in ("C:", "D:", "E:"):
        for parent in (Path(f"{drive}\\"), Path(f"{drive}\\Tools"), Path(f"{drive}\\Downloads")):
            if not parent.is_dir():
                continue
            try:
                for child in parent.iterdir():
                    if child.is_dir() and child.name.lower().startswith("napcat"):
                        yield child
            except OSError:
                continue


def _looks_like_shell(path: Path) -> bool:
    """判断一个目录是不是 NapCat 的 Shell 安装根。

    必须同时具备 QQ.exe 和 versions 目录——光有 napcat.bat 之类的标记文件不够，
    一键安装包解压出来的 bootmain/ 子目录也会同名文件，容易被误判。
    """
    if not path.is_dir():
        return False
    if path.name.lower() == "bootmain":
        return False
    has_qq = (path / "QQ.exe").is_file()
    has_versions = (path / "versions").is_dir()
    has_launcher = any((path / marker).is_file() for marker in ("napcat.bat", "launcher.bat", "NapCatWinBootMain.exe"))
    return has_qq and has_versions and has_launcher


def _read_version(shell: Path) -> str:
    for candidate in (shell / "package.json", shell / "versions" / "config.json"):
        if not candidate.is_file():
            continue
        try:
            data = json.loads(candidate.read_text(encoding="utf-8", errors="replace"))
        except (json.JSONDecodeError, OSError):
            continue
        if isinstance(data, dict) and data.get("version"):
            return str(data["version"])
    # 从版本目录名推断，如 versions\9.9.33-52230
    versions = shell / "versions"
    if versions.is_dir():
        dirs = [d.name for d in versions.iterdir() if d.is_dir()]
        if dirs:
            return sorted(dirs)[-1]
    return ""


def _find_config_dir(shell: Path) -> Path | None:
    """定位 NapCat 的 config 目录（一键安装版藏在 versions 下）。"""
    direct = shell / "config"
    if direct.is_dir():
        return direct
    versions = shell / "versions"
    if versions.is_dir():
        for child in sorted(versions.iterdir(), reverse=True):
            if not child.is_dir():
                continue
            for sub in ("resources/app/napcat/config", "resources/app/app_launcher/napcat/config"):
                candidate = child / sub
                if candidate.is_dir():
                    return candidate
    return None


def _find_launcher(shell: Path) -> Path | None:
    for name in ("napcat.bat", "launcher.bat", "launcher-win10.bat"):
        candidate = shell / name
        if candidate.is_file():
            return candidate
    return None


def find_install(hint: str | Path | None = None, ttl: float = 60.0) -> NapCatInstall | None:
    """尽力找到一份可用的 NapCat 安装。

    结果缓存 60 秒：安装目录不会频繁变化，而扫盘相对昂贵，
    界面每次刷状态都扫一遍没有必要。可传 ttl=0 强制重扫。
    """
    global _INSTALL_CACHE, _INSTALL_CACHE_AT
    now = time.time()
    if hint is None and _INSTALL_CACHE is not None and now - _INSTALL_CACHE_AT < ttl:
        return _INSTALL_CACHE

    found = _scan_install(hint)
    if hint is None:
        _INSTALL_CACHE, _INSTALL_CACHE_AT = found, now
    return found


def _scan_install(hint: str | Path | None = None) -> NapCatInstall | None:
    seen: set[str] = set()
    for root in _iter_candidate_roots(hint):
        if not root.is_dir():
            continue
        # 情况一：root 本身就是一个 Shell 目录
        candidates = [root] if _looks_like_shell(root) else []
        # 情况二：root 下面有 NapCat.*.Shell（一键安装）
        try:
            candidates += [
                child for child in root.iterdir()
                if child.is_dir() and _looks_like_shell(child)
            ]
        except OSError:
            pass

        for shell in candidates:
            key = str(shell.resolve()).lower()
            if key in seen:
                continue
            seen.add(key)
            autostart = shell / "napcat-autostart.bat"
            return NapCatInstall(
                root=shell.parent,
                shell=shell,
                version=_read_version(shell),
                config_dir=_find_config_dir(shell),
                launcher=_find_launcher(shell),
                autostart=autostart if autostart.is_file() else None,
            )
    return None


# ---------------------------------------------------------------- 进程与端口


def port_listening(port: int, host: str = "127.0.0.1", timeout: float = 1.0) -> bool:
    if not port:
        return False
    try:
        with socket.create_connection((host, int(port)), timeout=timeout):
            return True
    except OSError:
        return False


def napcat_processes(ttl: float = 5.0) -> list[dict[str, Any]]:
    """列出正在运行的 NapCat 相关进程（Windows）。

    启动一个 PowerShell 进程大约要 100~300 ms；界面每 3 秒轮询一次状态，
    如果每次都现查会白白烧 CPU。所以这里做 5 秒缓存，
    并且调用方应优先用"接口是否可用"来判断在不在线。
    """
    global _PROC_CACHE, _PROC_CACHE_AT
    now = time.time()
    if _PROC_CACHE is not None and now - _PROC_CACHE_AT < ttl:
        return _PROC_CACHE

    if os.name != "nt":
        _PROC_CACHE, _PROC_CACHE_AT = [], now
        return []

    procs: list[dict[str, Any]] = []
    try:
        import subprocess as sp

        out = sp.run(
            [
                "powershell", "-NoProfile", "-Command",
                "Get-Process -ErrorAction SilentlyContinue | "
                "Where-Object { $_.Path -like '*NapCat*' } | "
                "Select-Object ProcessName,Id,Path | ConvertTo-Json -Compress",
            ],
            capture_output=True, text=True, timeout=20,
        ).stdout.strip()
        if out:
            data = json.loads(out)
            if isinstance(data, dict):
                data = [data]
            procs = [
                {"name": item.get("ProcessName"), "pid": item.get("Id"), "path": item.get("Path")}
                for item in data
                if isinstance(item, dict)
            ]
    except (OSError, sp.SubprocessError, json.JSONDecodeError):
        procs = []

    _PROC_CACHE, _PROC_CACHE_AT = procs, now
    return procs


def read_qr_image(config_dir: Path | None) -> str | None:
    """读取 NapCat 生成的登录二维码，返回 data URL。"""
    if not config_dir:
        return None
    qr = config_dir.parent / "cache" / "qrcode.png"
    if not qr.is_file():
        return None
    for attempt in range(3):
        try:
            raw = qr.read_bytes()
        except OSError:
            time.sleep(0.3)
            continue
        if raw:
            return "data:image/png;base64," + base64.b64encode(raw).decode("ascii")
    return None


def qr_age_seconds(config_dir: Path | None) -> float | None:
    if not config_dir:
        return None
    qr = config_dir.parent / "cache" / "qrcode.png"
    try:
        return time.time() - qr.stat().st_mtime
    except OSError:
        return None


def tail_log(log_path: Path | None, lines: int = 30) -> list[str]:
    if not log_path or not log_path.is_file():
        return []
    try:
        text = log_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return []
    return text.splitlines()[-lines:]


def log_mentions_qr(lines: list[str]) -> bool:
    return any(("二维码" in line) or ("qrcode" in line.lower()) for line in lines)


# ---------------------------------------------------------------- 高层操作


class NapCatManager:
    """把 NapCat 的查找/启动/状态/登录包装成界面可用的接口。"""

    def __init__(self, hint: str | Path | None = None) -> None:
        self.hint = hint
        self._started_at: float = 0.0

    # ---- 定位 ----
    def install(self) -> NapCatInstall | None:
        return find_install(self.hint)

    def info(self) -> dict[str, Any]:
        inst = self.install()
        if not inst:
            procs = napcat_processes()
            return {
                "found": False,
                "running": bool(procs),
                "processes": procs,
                "message": "没有找到 NapCat 安装目录，请先按 docs\\NapCat配置指南.md 安装",
            }
        data = inst.as_dict()
        data.update(
            found=True,
            running=bool(napcat_processes()),
            autostart_configured=self._autostart_configured(),
        )
        return data

    @staticmethod
    def _autostart_configured() -> bool:
        startup = Path(os.environ.get("APPDATA", "")) / "Microsoft/Windows/Start Menu/Programs/Startup"
        if not startup.is_dir():
            return False
        return any("napcat" in p.name.lower() for p in startup.glob("*.lnk"))

    # ---- 启动 ----
    def start(self, wait_seconds: float = 90.0, probe_port: int = 0) -> dict[str, Any]:
        """启动 NapCat（优先用免扫码的自启脚本），并等待端口就绪。"""
        inst = self.install()
        if not inst:
            return {"ok": False, "message": "没有找到 NapCat 安装目录"}

        if napcat_processes():
            return {
                "ok": True,
                "already_running": True,
                "message": "NapCat 已经在运行",
                "shell": str(inst.shell),
            }

        launcher = inst.autostart or inst.launcher
        if not launcher:
            return {"ok": False, "message": f"在 {inst.shell} 里找不到启动脚本"}

        try:
            subprocess.Popen(
                ["cmd.exe", "/c", str(launcher)],
                cwd=str(inst.shell),
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                close_fds=True,
            )
        except OSError as exc:
            return {"ok": False, "message": f"启动失败：{exc}"}

        self._started_at = time.time()

        deadline = time.time() + max(10.0, wait_seconds)
        while time.time() < deadline:
            if probe_port and port_listening(probe_port):
                return {
                    "ok": True,
                    "message": "NapCat 已启动，OneBot 服务已就绪",
                    "waited": round(time.time() - self._started_at, 1),
                    "shell": str(inst.shell),
                }
            if not probe_port and napcat_processes():
                time.sleep(2.0)
                return {"ok": True, "message": "NapCat 已启动", "shell": str(inst.shell)}
            time.sleep(1.5)

        return {
            "ok": False,
            "message": (
                "已尝试启动 NapCat，但等待超时。请查看 Shell 目录下弹出的窗口，"
                "如果是首次登录需要扫码；也可以直接双击 napcat.bat 手动启动。"
            ),
            "shell": str(inst.shell),
        }

    def stop(self) -> dict[str, Any]:
        """结束 NapCat 进程（用于需要重启的场景）。"""
        procs = napcat_processes()
        if not procs:
            return {"ok": True, "message": "NapCat 未在运行"}
        killed = 0
        for item in procs:
            pid = item.get("pid")
            if not pid:
                continue
            try:
                subprocess.run(
                    ["taskkill", "/pid", str(pid), "/f"],
                    capture_output=True, timeout=15,
                )
                killed += 1
            except (OSError, subprocess.SubprocessError):
                continue
        time.sleep(2.0)
        return {"ok": True, "message": f"已结束 {killed} 个 NapCat 进程"}

    # ---- 登录状态 ----
    def login_state(self, base_url: str, token: str = "", timeout: float = 6.0) -> dict[str, Any]:
        inst = self.install()
        config_dir = inst.config_dir if inst else None
        state: dict[str, Any] = {
            "logged_in": False,
            "running": False,
            "bot": None,
            "known_accounts": [],
            "qr_available": False,
            "qr_age": None,
        }

        state["qr_available"] = bool(read_qr_image(config_dir))
        state["qr_age"] = qr_age_seconds(config_dir)

        # 谁曾经登录过？看有没有 onebot11_<QQ>.json（登录后才生成），
        # 这只能说明"登录过"，不能说明"此刻在线"，所以仅作为快速登录候选参考。
        if config_dir and config_dir.is_dir():
            accounts = [
                p.name[len("onebot11_"):-len(".json")]
                for p in config_dir.glob("onebot11_*.json")
                if p.name.startswith("onebot11_")
            ]
            state["known_accounts"] = sorted(set(accounts))

        # 是否真的登录成功，以接口返回为准
        try:
            client = OneBotClient(base_url, token, timeout=timeout)
            info = client.call("get_login_info") or {}
        except Exception as exc:  # noqa: BLE001
            # 接口不通时才需要确认"进程在不在"——这能区分
            # "NapCat 没启动" 和 "启动了但 OneBot 服务没开"
            state["running"] = bool(napcat_processes())
            state["message"] = str(exc)
            return state

        # 接口答了，说明进程一定在跑（省掉一次 PowerShell 进程枚举）
        state["running"] = True
        if isinstance(info, dict) and info.get("user_id"):
            state["logged_in"] = True
            state["bot"] = {"user_id": str(info.get("user_id")), "nickname": info.get("nickname")}
        else:
            state["message"] = "机器人已连接，但尚未登录 QQ，需要扫码"
        return state

    def qr_image(self) -> str | None:
        inst = self.install()
        return read_qr_image(inst.config_dir if inst else None)
