"""界面后端服务层：把抽奖内核包装成界面可直接调用的接口。

这一层不涉及任何 HTTP 细节，方便单独测试。
"""

from __future__ import annotations

import html
import json
import random
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from . import paths
from .archive import MessageArchive, message_key
from .config import Config, ConfigError, to_user_id
from .lottery import DrawContext, build_context, draw_winners, fairness_report, run_draw
from .model import Activity, Message
from .napcat import NapCatManager, qr_age_seconds
from .report import render_group_message, render_json
from .settings import ConfigFile
from .sources import (
    OneBotClient,
    OneBotError,
    load_from_file,
    load_from_onebot,
    looks_like_bot,
    sending_blocked,
)


@dataclass
class Result:
    """统一的调用结果。"""

    ok: bool = True
    message: str = ""
    data: Any = None
    code: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "message": self.message, "data": self.data, "code": self.code}


def ready_to_pull(cfg: Config) -> str:
    """拉消息前的前置检查。

    返回空字符串表示没问题；否则返回一句人话，说明缺什么。
    故意做成**只依赖传入的 Config**，这样 CLI（用 -c 指定配置）和界面
    都能安全复用，不会误读别的配置文件。
    """
    if cfg.source == "onebot" and not cfg.group_id:
        return (
            "还没有选择群号：请在界面左侧「抽奖群」里选一个，或直接点「一键开跑」自动选择；"
            "命令行请加上 --group 群号"
        )
    return ""


def _human_seconds(seconds: float) -> str:
    if seconds >= 3600:
        return f"{seconds / 3600:.1f} 小时"
    if seconds >= 60:
        return f"{seconds / 60:.0f} 分钟"
    return f"{seconds:.0f} 秒"


def _ts(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M")


def _is_local_endpoint(url: str) -> bool:
    """判断 OneBot 地址是否为本机或内网，用于给「发送」动作加保险。"""
    from urllib.parse import urlparse

    try:
        host = (urlparse(url).hostname or "").strip().lower()
    except ValueError:
        return False
    if not host:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0"):
        return True
    if host.startswith("127.") or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        first, second = int(parts[0]), int(parts[1])
        # 10.0.0.0/8、172.16.0.0/12、192.168.0.0/16
        if first == 10:
            return True
        if first == 172 and 16 <= second <= 31:
            return True
        if first == 192 and second == 168:
            return True
    return False


def _ts_full(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def _human_days(days: float) -> str:
    if days >= 1:
        text = f"{days:g} 天"
    else:
        hours = days * 24
        text = f"{hours:.4g} 小时"
    return text


class LotteryService:
    """界面调用的业务对象。"""

    def __init__(self, config_file: ConfigFile | None = None) -> None:
        self.config_file = config_file or ConfigFile()
        self._messages: list[Message] = []
        self._messages_meta: dict[str, Any] = {}
        self._last_draw: dict[str, Any] | None = None
        self.napcat = NapCatManager()
        self._archives: dict[str, MessageArchive] = {}
        self._last_auto_sync: float = 0.0
        # 自动存档跑在后台线程里，避免阻塞 HTTP 请求
        self._sync_lock = threading.Lock()
        self._sync_thread: threading.Thread | None = None

    # ------------------------------------------------------------ 消息存档
    def archive_for(self, cfg: Config | None = None) -> MessageArchive:
        """按配置拿存档对象（同一路径复用同一实例，避免重复打开数据库）。"""
        if cfg is None:
            try:
                cfg = self.config_file.to_config()
            except ConfigError:
                cfg = Config()
        key = str(cfg.archive_path_abs)
        if key not in self._archives:
            self._archives[key] = MessageArchive(cfg.archive_path_abs, enabled=cfg.archive_enabled)
        return self._archives[key]

    def close_archives(self) -> None:
        """关掉所有存档连接（退出前调用，确保 WAL 落盘）。"""
        for archive in list(self._archives.values()):
            archive.close()
        self._archives.clear()

    def archive_status(self) -> Result:
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        status = self.archive_for(cfg).status(cfg.group_id if cfg.source == "onebot" else "")
        data = status.as_dict()
        data["interval_seconds"] = cfg.archive_interval
        data["interval_label"] = _human_seconds(cfg.archive_interval)
        data["auto_sync"] = cfg.archive_auto_sync
        data["source_is_bot"] = cfg.source == "onebot"
        return Result(data=data)

    def sync_archive(self, silent: bool = False) -> Result:
        """把机器人当前能捞到的消息增量写入本地存档。"""
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        if not cfg.archive_enabled:
            return Result(ok=False, message="消息存档未启用", code="archive_disabled")
        if cfg.source != "onebot":
            return Result(ok=False, message="只有机器人数据源才能同步存档", code="not_onebot")
        if not cfg.group_id:
            return Result(ok=False, message="还没选群号，无法同步", code="group_missing")

        archive = self.archive_for(cfg)
        started = time.time()
        try:
            fresh = load_from_onebot(cfg, since=time.time() - cfg.window_seconds)
        except OneBotError as exc:
            archive.log_sync(started, {}, ok=False, message=str(exc))
            return Result(ok=False, message=f"同步失败：{exc}", code="onebot_error")

        result = archive.upsert(fresh, cfg.group_id)
        total = archive.count(cfg.group_id)
        result["total_after"] = total
        archive.log_sync(started, result, ok=True)
        self._last_auto_sync = time.time()

        status = archive.status(cfg.group_id).as_dict()
        return Result(
            message=(
                f"同步完成：抓取 {result['fetched']} 条，"
                f"新增 {result['inserted']} 条，已在库 {total} 条"
            ),
            data={"sync": result, "archive": status, "silent": silent},
        )

    def maybe_auto_sync(self) -> None:
        """按配置的间隔触发一次后台同步。

        必须放后台线程：一次同步要拉取上千条消息并写库（实测约 4 秒），
        同步执行会把处理 HTTP 请求的线程阻塞住，界面每 3 秒一次的轮询就会卡顿。
        """
        try:
            cfg = self.config_file.to_config()
        except ConfigError:
            return
        if not (cfg.archive_enabled and cfg.archive_auto_sync):
            return
        if cfg.source != "onebot" or not cfg.group_id:
            return
        if time.time() - self._last_auto_sync < cfg.archive_interval:
            return

        with self._sync_lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return  # 上一轮还没跑完，别叠加
            self._last_auto_sync = time.time()
            self._sync_thread = threading.Thread(
                target=self._background_sync, name="qq-lottery-auto-sync", daemon=True
            )
            self._sync_thread.start()

    def _background_sync(self) -> None:
        try:
            self.sync_archive(silent=True)
        except Exception:  # noqa: BLE001 后台线程绝不能把异常抛出去
            pass

    # ------------------------------------------------------------ 机器人管理
    def bot_info(self) -> Result:
        """NapCat 安装/运行/自启状态，供界面显示。"""
        try:
            return Result(data=self.napcat.info())
        except Exception as exc:  # noqa: BLE001
            return Result(ok=False, message=f"读取 NapCat 状态失败：{exc}", code="napcat_error")

    def bot_start(self) -> Result:
        """启动 NapCat（未启动时），并等待 OneBot 端口就绪。"""
        try:
            cfg = self.config_file.to_config()
            port = 0
            from urllib.parse import urlparse

            if cfg.source == "onebot":
                port = urlparse(cfg.onebot_base_url).port or 0
            outcome = self.napcat.start(probe_port=port)
        except ConfigError:
            outcome = self.napcat.start(probe_port=0)
        except Exception as exc:  # noqa: BLE001
            return Result(ok=False, message=f"启动 NapCat 失败：{exc}", code="napcat_error")
        if not outcome.get("ok"):
            return Result(ok=False, message=outcome.get("message", "启动失败"), code="napcat_start_failed", data=outcome)
        return Result(message=outcome.get("message", "已启动"), data=outcome)

    def bot_stop(self) -> Result:
        outcome = self.napcat.stop()
        return Result(ok=outcome.get("ok", True), message=outcome.get("message", ""))

    def bot_qr(self) -> Result:
        """取登录二维码（data URL）及其时效。"""
        image = self.napcat.qr_image()
        if not image:
            return Result(ok=False, message="暂时没有二维码，请先启动 NapCat 并稍等几秒", code="no_qr")
        inst = self.napcat.install()
        age = qr_age_seconds(inst.config_dir) if inst else None
        return Result(data={"image": image, "age_seconds": age})

    def bot_login_state(self) -> Result:
        """登录状态轮询：已登录 / 需要扫码 / 运行中但未登录。"""
        try:
            cfg = self.config_file.to_config()
            base = cfg.onebot_base_url
            token = cfg.onebot_access_token
        except ConfigError:
            base, token = "http://127.0.0.1:3000", ""

        state = self.napcat.login_state(base, token)
        state["send_disabled"] = sending_blocked()
        # 界面每 3 秒会轮询这里，正好用它驱动「按间隔自动存档」
        try:
            self.maybe_auto_sync()
        except Exception:  # noqa: BLE001
            pass
        # 带上安装信息，界面才能区分"没装 NapCat"和"装了但没登录"
        try:
            info = self.napcat.info()
            state["found"] = bool(info.get("found"))
            state["shell"] = info.get("shell", "")
            state["autostart_configured"] = info.get("autostart_configured", False)
            if not state.get("running"):
                state["running"] = bool(info.get("running"))
        except Exception:  # noqa: BLE001
            state["found"] = False
        # 附上二维码，界面可直接显示
        image = self.napcat.qr_image()
        if image and not state.get("logged_in"):
            state["qr_image"] = image
        return Result(data=state)

    def bot_groups(self) -> Result:
        """取机器人加入的群列表（优先用配置里的地址）。"""
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        client = OneBotClient(cfg.onebot_base_url, cfg.onebot_access_token, timeout=15.0)
        try:
            raw = client.call("get_group_list") or []
        except OneBotError as exc:
            return Result(ok=False, message=str(exc), code="onebot_error")
        groups = []
        for item in raw if isinstance(raw, list) else []:
            gid = to_user_id(item.get("group_id"))
            if not gid:
                continue
            groups.append(
                {
                    "group_id": gid,
                    "group_name": str(item.get("group_name") or gid),
                    "member_count": int(item.get("member_count") or 0),
                }
            )
        groups.sort(key=lambda g: -g["member_count"])
        return Result(message=f"机器人加入了 {len(groups)} 个群", data=groups)

    # ------------------------------------------------------------ 一键开跑
    def run_all(self, payload: dict[str, Any] | None = None) -> Result:
        """一步到位：确保机器人在线 → 选定群 → 刷新数据 → 直接开奖。

        注意：**这一步不会往群里发任何消息**。发送必须由用户单独点击。
        """
        payload = payload or {}
        steps: list[dict[str, Any]] = []

        def step(name: str, ok: bool, message: str) -> None:
            steps.append({"name": name, "ok": ok, "message": message})

        # 1) 机器人在线
        cfg = self.config_file.to_config()
        if cfg.source != "onebot":
            step("检查数据源", False, "当前数据源不是机器人接口，请先切换")
            return Result(ok=False, message="当前数据源不是机器人接口", code="not_onebot", data={"steps": steps})
        client = OneBotClient(cfg.onebot_base_url, cfg.onebot_access_token, timeout=8.0)
        online = False
        bot = None
        try:
            info = client.call("get_login_info") or {}
            if isinstance(info, dict) and info.get("user_id"):
                online = True
                bot = {"user_id": to_user_id(info.get("user_id")), "nickname": info.get("nickname")}
        except OneBotError as exc:
            step("连接机器人", False, str(exc))
            return Result(ok=False, message=str(exc), code="onebot_unreachable", data={"steps": steps})
        step("连接机器人", online, f"{bot['nickname']}({bot['user_id']})" if bot else "已连接")

        # 2) 选定群
        group_id = to_user_id(payload.get("group_id")) or cfg.group_id
        if not group_id:
            groups_res = self.bot_groups()
            groups = groups_res.data if groups_res.ok else []
            if not groups:
                step("选择群", False, "没有可用群，请手动填写群号")
                return Result(ok=False, message="没有可用群，请手动填写群号", code="no_group", data={"steps": steps})
            group_id = groups[0]["group_id"]
            step("选择群", True, f"自动选用人最多的群 {group_id}（{groups[0]['group_name']}）")
        else:
            step("选择群", True, group_id)

        # 3) 保存设置（数据源/群号/人数/范围）
        pool = str(payload.get("pool") or cfg.pool or "active").lower()
        if pool not in ("active", "all_members"):
            pool = "active"
        patch: dict[str, Any] = {"source": "onebot", "group_id": group_id, "pool": pool}
        if payload.get("winners") not in (None, ""):
            patch["winners"] = max(1, int(payload["winners"]))
        if payload.get("days") not in (None, ""):
            patch["days"] = float(payload["days"])
        if payload.get("window_mode"):
            patch["window_mode"] = str(payload["window_mode"])
        saved = self.save_config(patch)
        if not saved.ok:
            step("保存设置", False, saved.message)
            return Result(ok=False, message=saved.message, code=saved.code, data={"steps": steps})
        step("保存设置", True, f"群号 {group_id}")

        # 全群成员模式：不需要消息历史，直接读成员列表开奖
        if pool == "all_members":
            step("刷新数据", True, "全群模式无需读取消息历史")
            draw_res = self.draw_all_members({"winners": patch.get("winners")})
            if not draw_res.ok:
                step("开奖", False, draw_res.message)
                return Result(ok=False, message=draw_res.message, code=draw_res.code, data={"steps": steps})
            step("开奖", True, f"{len(draw_res.data['winners'])} 位群友中奖（全群等概率）")
            data = draw_res.data
            data["steps"] = steps
            data["group_id"] = group_id
            data["send_disabled"] = sending_blocked()
            data["posted"] = False
            return Result(message="开奖完成（尚未发送到群）", data=data)

        # 4) 拉取消息并统计
        stats_res = self.stats(force_reload=True)
        if not stats_res.ok:
            step("刷新数据", False, stats_res.message)
            return Result(ok=False, message=stats_res.message, code=stats_res.code, data={"steps": steps})
        summary = stats_res.data["summary"]
        step(
            "刷新数据",
            True,
            f"{summary['messages_in_window']} 条消息 / {summary['candidates']} 位候选群友",
        )

        # 5) 开奖（不发群）
        draw_res = self.draw({"winners": patch.get("winners"), "days": patch.get("days")})
        if not draw_res.ok:
            step("开奖", False, draw_res.message)
            return Result(ok=False, message=draw_res.message, code=draw_res.code, data={"steps": steps})
        step("开奖", True, f"{len(draw_res.data['winners'])} 位群友中奖")

        data = draw_res.data
        data["steps"] = steps
        data["group_id"] = group_id
        data["send_disabled"] = sending_blocked()
        data["posted"] = False
        return Result(message="开奖完成（尚未发送到群）", data=data)

    # ------------------------------------------------------------ 配置
    def get_config(self) -> Result:
        try:
            self.config_file.load()
            view = self.config_file.view()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        data_path = paths.resolve(view["file"]["path"])
        view["data_file_exists"] = data_path.is_file()
        view["data_file_size"] = data_path.stat().st_size if data_path.is_file() else 0
        return Result(data=view)

    def save_config(self, payload: dict[str, Any]) -> Result:
        if not isinstance(payload, dict):
            return Result(ok=False, message="提交的设置格式不正确", code="bad_request")
        try:
            # 关键：合并进磁盘上的真实配置，避免未提交字段被默认值覆盖
            self.config_file.merge(payload)
            # 保存后再校验一次，配置非法要立刻让界面知道
            self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        except OSError as exc:
            return Result(ok=False, message=f"写入 config.json 失败：{exc}", code="io_error")
        return Result(
            message=f"设置已保存到 {paths.display(self.config_file.path)}",
            data=self.config_file.view(),
        )

    def check_config(self) -> Result:
        """界面上的“连通性检查”。"""
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")

        if cfg.source == "file":
            path = cfg.file_path_abs
            if not path.is_file():
                return Result(ok=False, message=f"聊天记录文件不存在：{path}", code="file_missing")
            return Result(
                message=f"记录文件就绪（{path.stat().st_size / 1024:.0f} KB）",
                data={"source": "file", "path": str(path)},
            )

        if not cfg.group_id:
            return Result(ok=False, message="请先填写群号", code="group_missing")
        client = OneBotClient(cfg.onebot_base_url, cfg.onebot_access_token, timeout=8.0)
        try:
            login = client.call("get_login_info") or {}
        except OneBotError as exc:
            return Result(ok=False, message=str(exc), code="onebot_unreachable")
        nickname = login.get("nickname") if isinstance(login, dict) else ""
        user_id = to_user_id(login.get("user_id") if isinstance(login, dict) else None)
        return Result(
            message=f"已连接机器人 {nickname or user_id or '（未知）'}",
            data={"source": "onebot", "self_id": user_id, "nickname": nickname},
        )

    def check_ready_to_pull(self) -> Result:
        """拉消息前的友好检查：数据源与群号是否具备。

        CLI 和界面共用——这样"忘了填群号"得到的是一句人话，
        而不是机器人接口返回的莫名错误。
        """
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        problem = ready_to_pull(cfg)
        if problem:
            return Result(ok=False, message=problem, code="group_missing")
        return Result()

    def list_group_members(self) -> Result:
        """把群成员拉下来，方便在界面上勾选黑名单。"""
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        if cfg.source != "onebot" or not cfg.group_id:
            return Result(ok=False, message="只有 onebot 数据源才能读取群成员列表", code="not_onebot")
        client = OneBotClient(cfg.onebot_base_url, cfg.onebot_access_token, timeout=20.0)
        try:
            members = client.get_group_member_list(cfg.group_id)
        except OneBotError as exc:
            return Result(ok=False, message=str(exc), code="onebot_error")
        rows = []
        for item in members:
            uid = to_user_id(item.get("user_id"))
            if not uid:
                continue
            rows.append(
                {
                    "user_id": uid,
                    "nickname": str(item.get("card") or item.get("nickname") or uid),
                    "role": str(item.get("role") or "member"),
                }
            )
        rows.sort(key=lambda r: (r["role"] != "owner", r["role"] != "admin", r["nickname"]))
        return Result(message=f"共读取到 {len(rows)} 位群成员", data=rows)

    # ------------------------------------------------------------ 数据
    def load_messages(self, force: bool = False) -> Result:
        """读取消息（按配置缓存，配置不变就不重复拉取）。"""
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")

        signature = self._signature(cfg)
        if not force and self._messages and self._messages_meta.get("signature") == signature:
            # 命中缓存：返回真实条数，并标明这是缓存结果
            return Result(
                data={
                    **self._messages_meta,
                    "count": len(self._messages),
                    "cached": True,
                }
            )

        if cfg.source == "onebot" and not cfg.group_id:
            return Result(
                ok=False,
                message="还没有选择群号：请在左侧「抽奖群」里选一个，或直接点「一键开跑」自动选择",
                code="group_missing",
            )

        started = time.time()
        try:
            if cfg.source == "onebot":
                since = time.time() - cfg.window_seconds
                messages = load_from_onebot(cfg, since=since)
            else:
                messages = load_from_file(cfg.file_path_abs, cfg.file_format, cfg.group_id)
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        except OneBotError as exc:
            return Result(ok=False, message=str(exc), code="onebot_error")
        except FileNotFoundError as exc:
            return Result(ok=False, message=f"文件不存在：{exc}", code="file_missing")

        archive_used = 0
        archive_note = ""
        # 实时数据先落盘（只增不减），再和存档合并——
        # 于是窗口越长、攒得越久，历史越完整（不再受 NapCat 条数上限限制）
        if cfg.archive_enabled and cfg.source == "onebot":
            try:
                archive = self.archive_for(cfg)
                archive.upsert(messages, cfg.group_id)
                archived = archive.load(cfg.group_id, since=time.time() - cfg.window_seconds)
                if archived:
                    merged: dict[str, Message] = {}
                    for msg in archived:
                        merged[message_key(msg)] = msg
                    for msg in messages:
                        merged[message_key(msg)] = msg
                    archive_used = len(merged) - len({message_key(m) for m in messages})
                    messages = sorted(merged.values(), key=lambda m: m.timestamp)
            except (sqlite3.Error, OSError) as exc:
                # 存档坏了不能拖垮抽奖，但必须让人知道——静默失败会让人以为历史在攒
                archive_note = f"存档读写失败，本次只用实时数据：{exc}"
            except Exception as exc:  # noqa: BLE001  兜底，同样要留痕
                archive_note = f"存档出现意外问题，本次只用实时数据：{type(exc).__name__}: {exc}"

        if not messages:
            return Result(
                ok=False,
                message="没有读取到任何消息，请检查数据源设置",
                code="no_messages",
            )

        self._messages = messages
        self._messages_meta = {
            "signature": signature,
            "count": len(messages),
            "source": cfg.source,
            "loaded_at": _ts_full(time.time()),
            "elapsed": round(time.time() - started, 2),
            "earliest": _ts(messages[0].timestamp),
            "latest": _ts(messages[-1].timestamp),
            "cached": False,
            "archive_used": archive_used,
            "archive_note": archive_note,
        }
        return Result(data=dict(self._messages_meta))

    def _signature(self, cfg: Config) -> str:
        return "|".join(
            [
                cfg.source,
                cfg.group_id,
                cfg.file_path_abs.as_posix(),
                cfg.file_format,
                str(cfg.file_path_abs.stat().st_mtime_ns) if cfg.file_path_abs.is_file() else "0",
                cfg.onebot_base_url,
                str(cfg.onebot_fetch_limit),
                str(int(cfg.window_seconds)),
            ]
        )

    # ------------------------------------------------------------ 统计
    def stats(self, force_reload: bool = False, top: int = 200) -> Result:
        # 顺带按间隔做一次自动存档（时间到了才会真的同步）
        self.maybe_auto_sync()
        loaded = self.load_messages(force=force_reload)
        if not loaded.ok:
            return loaded
        cfg = self.config_file.to_config()
        ctx = build_context(cfg, self._messages)
        return Result(
            data={
                "meta": dict(self._messages_meta),
                "summary": self._summary(cfg, ctx),
                "top": self._ranking(cfg, ctx, top),
            }
        )

    def _summary(self, cfg: Config, ctx: DrawContext) -> dict[str, Any]:
        return {
            "source": cfg.source,
            "source_label": "机器人接口" if cfg.source == "onebot" else "离线记录",
            "group_id": cfg.group_id,
            "days": cfg.days,
            "days_label": f"近 {_human_days(cfg.days)}",
            "window": ctx.window_desc,
            "window_start": _ts_full(ctx.window_start),
            "window_end": _ts_full(ctx.window_end),
            "window_reference": ctx.reference,
            "messages_total": ctx.total_messages,
            "messages_in_window": ctx.in_window_messages,
            "speakers": len(ctx.activities),
            "candidates": len(ctx.candidates),
            "weighting": cfg.weighting,
            "weight_cap": cfg.weight_cap,
            "min_messages": cfg.min_messages,
            "winners": cfg.winners,
            "total_weight": round(sum(a.weight_input for a in ctx.candidates), 3),
            # 公平性前提：数据是否覆盖了整个统计窗口
            "coverage": {
                "complete": bool(ctx.coverage.get("complete")),
                "ratio": ctx.coverage.get("coverage", 0.0),
                "percent": round(ctx.coverage.get("coverage", 0.0) * 100, 1),
                "earliest": _ts(ctx.coverage["earliest"]) if ctx.coverage.get("earliest") else "",
                "missing_hours": round(ctx.coverage.get("missing_seconds", 0) / 3600, 2),
                "warning": ctx.coverage.get("message", ""),
            },
            "excluded": [
                {"reason": reason, "count": len(people), "people": people[:12]}
                for reason, people in ctx.excluded.items()
            ],
            "notes": ctx.notes,
            # 存档读写异常时让界面能提示（静默失败会让人误以为历史在攒）
            "archive_note": self._messages_meta.get("archive_note", ""),
            "archive_used": self._messages_meta.get("archive_used", 0),
        }

    def _ranking(self, cfg: Config, ctx: DrawContext, top: int) -> list[dict[str, Any]]:
        total = sum(a.weight_input for a in ctx.candidates) or 1.0
        rows = []
        for idx, act in enumerate(ctx.candidates[:top], start=1):
            rows.append(
                {
                    "rank": idx,
                    "user_id": act.user_id,
                    "nickname": act.nickname,
                    "role": act.role,
                    "count": act.count,
                    "weight": round(act.weight_input, 3),
                    "chance": round(act.weight_input / total, 6),
                    "chance_pct": f"{act.weight_input / total * 100:.2f}%",
                    "last_speak": _ts(act.last_ts),
                    "first_speak": _ts(act.first_ts),
                    "is_bot": looks_like_bot(act.user_id, act.nickname),
                }
            )
        return rows

    # ------------------------------------------------------------ 开奖
    def draw(self, payload: dict[str, Any] | None = None) -> Result:
        payload = payload or {}
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")

        # 界面上的临时参数（不写盘）
        if payload.get("winners") not in (None, ""):
            try:
                cfg.winners = max(1, int(payload["winners"]))
            except (TypeError, ValueError):
                return Result(ok=False, message="抽取人数必须是整数", code="bad_request")
        if payload.get("days") not in (None, ""):
            try:
                cfg.days = float(payload["days"])
            except (TypeError, ValueError):
                return Result(ok=False, message="统计天数必须是数字", code="bad_request")
        if payload.get("window_mode"):
            mode = str(payload["window_mode"]).lower()
            if mode not in ("days", "auto"):
                return Result(ok=False, message="统计窗口模式只能是 days 或 auto", code="bad_request")
            cfg.window_mode = mode
        if payload.get("seed") not in (None, ""):
            try:
                cfg.seed = int(payload["seed"])
            except (TypeError, ValueError):
                return Result(ok=False, message="随机种子必须是整数", code="bad_request")
        else:
            cfg.seed = None
        if "weighting" in payload:
            cfg.weighting = bool(payload["weighting"])
        if "weight_cap" in payload:
            try:
                cfg.weight_cap = max(0, int(payload["weight_cap"]))
            except (TypeError, ValueError):
                return Result(ok=False, message="权重上限必须是整数", code="bad_request")

        try:
            cfg.validate()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")

        loaded = self.load_messages(force=bool(payload.get("reload")))
        if not loaded.ok:
            return loaded

        winners, ctx, seed = run_draw(cfg, self._messages)
        if not winners:
            return Result(
                ok=False,
                message=f"{_human_days(cfg.days)}内没有符合条件的群友，无法开奖",
                code="no_candidates",
                data={"summary": self._summary(cfg, ctx)},
            )

        record = render_json(cfg, ctx, winners, seed)
        record["_id"] = f"{datetime.now():%Y%m%d%H%M%S}-{seed % 100000:05d}"
        # 公平性记录：随机方式、种子来源、权重与概率、数据覆盖情况，全部留档可复核
        fairness = fairness_report(cfg, ctx, winners, seed, ctx.coverage)
        record["fairness"] = fairness
        self._last_draw = {
            "record": record,
            "cfg": cfg,
            "ctx": ctx,
            "winners": winners,
            "seed": seed,
        }

        history_path = cfg.history_file_abs
        saved_to = ""
        if history_path:
            try:
                saved_to = self._append_history(history_path, record)
            except OSError as exc:
                saved_to = f"写入失败：{exc}"

        return Result(
            data={
                "id": record["_id"],
                "drawn_at": record["drawn_at"],
                "seed": seed,
                "winners": [w.as_dict() for w in winners],
                "summary": self._summary(cfg, ctx),
                "ranking": self._ranking(cfg, ctx, 12),
                "group_message": render_group_message(cfg, ctx, winners),
                "history_saved_to": saved_to,
                "history_enabled": bool(history_path),
                "fairness": fairness,
            }
        )

    @staticmethod
    def _append_history(path: Path, record: dict[str, Any]) -> str:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False) + "\n")
        return paths.display(path)

    def draw_all_members(self, payload: dict[str, Any] | None = None) -> Result:
        """全群成员抽取：候选池 = 群里所有人，**每人等概率**。

        与「活跃群友」是两条完全不同的通路：
          · 活跃群友 -> 读群「消息」历史，按发言条数加权
          · 全群成员 -> 读群「成员列表」，每人权重恒为 1
        """
        payload = payload or {}
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        if cfg.source != "onebot" or not cfg.group_id:
            return Result(
                ok=False,
                message="全群抽取需要机器人数据源并选定群号（群成员列表只能通过机器人接口获取）",
                code="not_onebot",
            )

        if payload.get("winners") not in (None, ""):
            try:
                cfg.winners = max(1, int(payload["winners"]))
            except (TypeError, ValueError):
                return Result(ok=False, message="抽取人数必须是整数", code="bad_request")
        if payload.get("seed") not in (None, ""):
            try:
                cfg.seed = int(payload["seed"])
            except (TypeError, ValueError):
                return Result(ok=False, message="随机种子必须是整数", code="bad_request")
        else:
            cfg.seed = None

        members_res = self.list_group_members()
        if not members_res.ok:
            return members_res
        members: list[dict[str, Any]] = members_res.data or []
        if not members:
            return Result(ok=False, message=f"群 {cfg.group_id} 没有读到任何成员", code="no_members")

        blacklist = set(cfg.blacklist)
        exclude_roles = set(cfg.exclude_roles)
        pool: list[Activity] = []
        excluded_role = excluded_bot = excluded_black = 0
        for item in members:
            uid = str(item.get("user_id") or "")
            name = str(item.get("nickname") or uid)
            role = str(item.get("role") or "member")
            if not uid:
                continue
            if uid in blacklist:
                excluded_black += 1
                continue
            if looks_like_bot(uid, name):
                excluded_bot += 1
                continue
            if role and role in exclude_roles:
                excluded_role += 1
                continue
            pool.append(Activity(user_id=uid, nickname=name, count=0, role=role, weight_input=1.0))

        if not pool:
            return Result(ok=False, message="过滤后没有可参与抽奖的成员", code="no_candidates")

        seed = cfg.seed
        if seed is None:
            seed = random.SystemRandom().getrandbits(63)
        rng = random.Random(int(seed))
        winners = draw_winners(pool, cfg.winners, cfg, rng)
        if not winners:
            return Result(ok=False, message="没有抽出中奖者", code="no_candidates")

        drawn_at = datetime.now()
        record = {
            "_id": f"{drawn_at:%Y%m%d%H%M%S}-{int(seed) % 100000:05d}",
            "drawn_at": _ts_full(drawn_at.timestamp()),
            "pool": "all_members",
            "pool_label": "全群成员（每人等概率）",
            "group_id": cfg.group_id,
            "source": cfg.source,
            "member_total": len(members),
            "candidate_count": len(pool),
            "seed": int(seed),
            "winners_requested": cfg.winners,
            "winners": [w.as_dict() for w in winners],
            "excluded": {
                "黑名单": excluded_black,
                "疑似机器人": excluded_bot,
                "按身份排除": excluded_role,
            },
            "fairness": {
                "method": "uniform_random_sampling_without_replacement",
                "method_label": "全群成员每人等概率，无放回抽取（一人最多中一次）",
                "algorithm": "each-round draws r ~ U(0, total_weight) and walks cumulative weights",
                "reproducible": True,
                "seed": int(seed),
                "seed_source": "配置固定种子" if cfg.seed is not None else "系统加密随机源（每次不同）",
                "candidate_count": len(pool),
                "total_weight": float(len(pool)),
                "dedup": True,
                "note": (
                    "候选池来自群成员列表，与发言多少无关；每人权重都是 1，"
                    "因此概率完全相等。潜水成员同样有机会。"
                ),
            },
        }
        self._last_draw = {
            "record": record,
            "cfg": cfg,
            "ctx": None,
            "winners": winners,
            "seed": int(seed),
            "pool": "all_members",
        }

        history_path = cfg.history_file_abs
        saved_to = ""
        if history_path:
            try:
                saved_to = self._append_history(history_path, record)
            except OSError as exc:
                saved_to = f"写入失败：{exc}"

        total = max(len(pool), 1)
        return Result(
            data={
                "id": record["_id"],
                "drawn_at": record["drawn_at"],
                "pool": "all_members",
                "seed": int(seed),
                "winners": [w.as_dict() for w in winners],
                "member_total": len(members),
                "candidate_count": len(pool),
                "excluded_summary": record["excluded"],
                "fairness": record["fairness"],
                "group_message": self._render_all_members_message(cfg, winners, len(pool)),
                "history_saved_to": saved_to,
                "history_enabled": bool(history_path),
            }
        )

    @staticmethod
    def _render_all_members_message(cfg: Config, winners: Sequence[Winner], pool_size: int) -> str:
        lines = ["🎉 本群抽奖开奖啦"]
        lines.append(f"范围：全群成员（共 {pool_size} 人参与，每人等概率）")
        lines.append("规则：不看活跃度，人人机会相同，每人最多中一次")
        lines.append("———————————")
        icons = ["🥇", "🥈", "🥉"]
        for win in winners:
            icon = icons[win.rank - 1] if win.rank <= len(icons) else "🎁"
            lines.append(f"{icon} {win.nickname}（{win.user_id}）")
        lines.append("———————————")
        lines.append("恭喜以上群友，请私聊领取奖励～")
        return "\n".join(lines)

    def post_to_group(self, text: str | None = None) -> Result:
        """把开奖结果发到群里。

        这是本工具唯一会"向外部真实群发消息"的动作，所以加了两道保险：
          1. QQLOTTERY_NO_SEND 环境变量被置位时直接拒绝（测试用）
          2. 只允许发给本机/内网地址，避免误发到公网第三方服务器
        """
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        if cfg.source != "onebot" or not cfg.group_id:
            return Result(ok=False, message="发群需要 onebot 数据源并填写群号", code="not_onebot")

        if not _is_local_endpoint(cfg.onebot_base_url):
            return Result(
                ok=False,
                message=(
                    f"出于安全考虑，只允许向本机或内网地址发送消息，当前为 {cfg.onebot_base_url}。"
                    "如果你确实要发给远程机器人，请手动发送。"
                ),
                code="remote_endpoint",
            )

        if not text:
            if not self._last_draw:
                return Result(ok=False, message="还没有开奖结果可发送", code="no_draw")
            if self._last_draw.get("pool") == "all_members":
                text = self._render_all_members_message(
                    self._last_draw["cfg"],
                    self._last_draw["winners"],
                    int((self._last_draw.get("record") or {}).get("candidate_count") or 0),
                )
            else:
                text = render_group_message(
                    self._last_draw["cfg"], self._last_draw["ctx"], self._last_draw["winners"]
                )
        client = OneBotClient(cfg.onebot_base_url, cfg.onebot_access_token, timeout=15.0)
        try:
            client.send_group_msg(cfg.group_id, text)
        except OneBotError as exc:
            return Result(ok=False, message=str(exc), code="onebot_error")
        except Exception as exc:  # SendBlockedError 等安全拦截
            return Result(ok=False, message=str(exc), code="send_blocked")
        return Result(message="开奖结果已发送到群")

    # ------------------------------------------------------------ 历史
    def history(self, limit: int = 50) -> Result:
        try:
            cfg = self.config_file.to_config()
        except ConfigError as exc:
            return Result(ok=False, message=str(exc), code="config_invalid")
        path = cfg.history_file_abs
        if not path or not path.is_file():
            return Result(data={"items": [], "path": paths.display(cfg.history_file), "exists": False})

        items: list[dict[str, Any]] = []
        with path.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    items.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        items.reverse()
        trimmed = []
        for item in items[: max(1, limit)]:
            trimmed.append(
                {
                    "id": item.get("_id") or item.get("drawn_at", ""),
                    "drawn_at": item.get("drawn_at", ""),
                    "seed": item.get("seed"),
                    "days": item.get("days"),
                    "source": item.get("source"),
                    "group_id": item.get("group_id"),
                    "candidate_count": item.get("candidate_count"),
                    "winners": item.get("winners") or [],
                }
            )
        return Result(
            data={
                "items": trimmed,
                "path": paths.display(path),
                "exists": True,
                "total": len(items),
            }
        )

    # ------------------------------------------------------------ 导出
    def export(self, kind: str = "csv") -> Result:
        """导出本次开奖结果。kind: csv / json / poster（HTML 海报）。"""
        if not self._last_draw:
            return Result(ok=False, message="还没有开奖结果可导出", code="no_draw")
        record = self._last_draw["record"]
        out_dir = paths.resolve("out")
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = f"{datetime.now():%Y%m%d-%H%M%S}"

        if kind == "csv":
            target = out_dir / f"中奖名单-{stamp}.csv"
            self._write_csv(target, record)
            return Result(message="已导出 CSV", data={"file": target.name, "path": str(target)})
        if kind == "json":
            target = out_dir / f"开奖结果-{stamp}.json"
            target.write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
            return Result(message="已导出 JSON", data={"file": target.name, "path": str(target)})
        if kind == "poster":
            target = out_dir / f"中奖海报-{stamp}.html"
            target.write_text(self._render_poster(record), encoding="utf-8")
            return Result(
                message="已导出海报（HTML，用浏览器打开后可直接另存为图片）",
                data={"file": target.name, "path": str(target), "openable": True},
            )
        return Result(ok=False, message=f"不支持的导出类型：{kind}", code="bad_request")

    @staticmethod
    def _write_csv(target: Path, record: dict[str, Any]) -> None:
        import csv as _csv

        with target.open("w", encoding="utf-8-sig", newline="") as fh:
            writer = _csv.writer(fh)
            writer.writerow(["名次", "QQ号", "昵称", "发言条数", "中奖概率", "最后发言", "开奖时间", "随机种子"])
            for win in record.get("winners", []):
                writer.writerow(
                    [
                        win.get("rank"),
                        win.get("user_id"),
                        win.get("nickname"),
                        win.get("count"),
                        win.get("probability_pct"),
                        win.get("last_speak"),
                        record.get("drawn_at"),
                        record.get("seed"),
                    ]
                )

    def _render_poster(self, record: dict[str, Any]) -> str:
        """生成一张可直接截图/打印的中奖海报（纯 HTML，无外部依赖）。"""
        winners = record.get("winners", [])
        window = record.get("window", {})
        days = record.get("days", 3)
        cards = []
        for win in winners:
            cards.append(
                f"""
      <li class="winner">
        <span class="rank">{html.escape(str(win.get('rank')))}</span>
        <span class="name">{html.escape(str(win.get('nickname', '')))}</span>
        <span class="uid">{html.escape(str(win.get('user_id', '')))}</span>
        <span class="stat">{win.get('count', 0)} 条 · {html.escape(str(win.get('probability_pct', '')))}</span>
      </li>"""
            )
        return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>中奖海报</title>
<style>
  * {{ box-sizing: border-box; }}
  body {{
    margin: 0; min-height: 100vh; display: grid; place-items: center;
    font-family: "Microsoft YaHei", "PingFang SC", system-ui, sans-serif;
    background: radial-gradient(1200px 600px at 20% 0%, #1f3a5f 0%, #0f1c2e 55%, #0a1320 100%);
    padding: 40px;
  }}
  .poster {{
    width: 720px; padding: 44px 48px 36px; border-radius: 28px; color: #eef4ff;
    background: linear-gradient(160deg, rgba(255,255,255,.10), rgba(255,255,255,.03));
    border: 1px solid rgba(255,255,255,.16);
    box-shadow: 0 30px 80px rgba(0,0,0,.55), inset 0 1px 0 rgba(255,255,255,.18);
    backdrop-filter: blur(6px);
  }}
  .badge {{ display:inline-block; padding:6px 14px; border-radius:999px; font-size:13px; letter-spacing:.08em;
    background: rgba(94,234,212,.14); color:#5eead4; border:1px solid rgba(94,234,212,.35); }}
  h1 {{ margin: 18px 0 6px; font-size: 40px; letter-spacing: .02em; }}
  .sub {{ color:#9fb3cd; font-size:14px; line-height:1.9; }}
  ul {{ list-style:none; margin:26px 0 8px; padding:0; display:grid; gap:10px; }}
  .winner {{ display:grid; grid-template-columns: 46px 1fr auto; grid-template-areas: "rank name stat" "rank uid stat";
    align-items:center; gap:2px 12px; padding:14px 18px; border-radius:16px;
    background: linear-gradient(100deg, rgba(94,234,212,.13), rgba(94,234,212,.03));
    border:1px solid rgba(94,234,212,.22); }}
  .winner .rank {{ grid-area: rank; font-size:22px; font-weight:700; color:#fbbf24; text-align:center; }}
  .winner .name {{ grid-area: name; font-size:20px; font-weight:600; }}
  .winner .uid {{ grid-area: uid; font-size:13px; color:#9fb3cd; font-variant-numeric: tabular-nums; }}
  .winner .stat {{ grid-area: stat; font-size:13px; color:#a7f3d0; }}
  .foot {{ margin-top: 26px; padding-top: 18px; border-top: 1px dashed rgba(255,255,255,.16);
    display:flex; justify-content:space-between; font-size:12.5px; color:#8ea4c0; }}
</style>
</head>
<body>
  <div class="poster">
    <span class="badge">QQ 群抽奖 · 开奖结果</span>
    <h1>🎉 恭喜以下群友中奖</h1>
    <div class="sub">
      参与范围：近 {days:g} 天内发过言的群友，共 {record.get('candidate_count', 0)} 人参与<br>
      统计窗口：{html.escape(str(window.get('start', '')))} ~ {html.escape(str(window.get('end', '')))}<br>
      抽奖方式：{'按发言条数加权' if record.get('weighting') else '每人等概率'} · 开奖时间 {html.escape(str(record.get('drawn_at', '')))}
    </div>
    <ul>{''.join(cards)}</ul>
    <div class="foot">
      <span>随机种子 {html.escape(str(record.get('seed', '')))}（可用于复现本次结果）</span>
      <span>共 {len(winners)} 位中奖者</span>
    </div>
  </div>
</body>
</html>
"""

    # ------------------------------------------------------------ 供界面读取
    def last_draw(self) -> dict[str, Any] | None:
        if not self._last_draw:
            return None
        record = self._last_draw["record"]
        return {"id": record.get("_id"), "drawn_at": record.get("drawn_at")}

    def snapshot(self) -> dict[str, Any]:
        """轻量状态，供界面轮询。"""
        return {
            "has_messages": bool(self._messages),
            "messages": self._messages_meta,
            "last_draw": self.last_draw(),
        }
