"""配置加载与合并。

优先级：命令行参数 > 配置文件 > 内置默认值。
配置文件为 JSON（纯标准库解析，无需安装依赖）。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import paths

DEFAULT_CONFIG_NAME = "config.json"

DEFAULTS: dict[str, Any] = {
    "source": "file",
    "group_id": "",
    "days": 3,
    "window_mode": "days",
    "pool": "active",
    "archive": {
        "enabled": True,
        "path": "data/archive.db",
        "sync_interval_seconds": 300,
        "auto_sync": True,
    },
    "winners": 1,
    "weighting": True,
    "weight_cap": 0,
    "min_messages": 1,
    "exclude_roles": [],
    "blacklist": [],
    "whitelist": [],
    "seed": None,
    "post_to_group": False,
    "history_file": "data/history.jsonl",
    "file": {
        "path": "data/messages.json",
        "format": "auto",
    },
    "onebot": {
        "base_url": "http://127.0.0.1:3000",
        "access_token": "",
        "fetch_limit": 30000,
        "page_size": 1500,
        "max_pages": 300,
        "exclude_self": True,
    },
}


class ConfigError(ValueError):
    """配置错误。"""


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = _deep_merge(out[key], value)
        else:
            out[key] = value
    return out


def parse_time_window(text: str) -> float:
    """把 '3d' / '72h' / '30m' / '3' 解析成秒。"""
    m = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*([a-zA-Z]*)\s*", text or "")
    if not m:
        raise ConfigError(f"无法解析时间窗口: {text!r}（示例：3d、72h、30m）")
    value = float(m.group(1))
    unit = (m.group(2) or "d").lower()
    factors = {"s": 1, "m": 60, "h": 3600, "d": 86400, "w": 604800}
    if unit not in factors:
        raise ConfigError(f"不支持的时间单位: {unit!r}（支持 s/m/h/d/w）")
    return value * factors[unit]


def to_user_id(value: Any) -> str:
    """把各种写法的 QQ 号统一成字符串。"""
    if value is None:
        return ""
    if isinstance(value, bool):
        return ""
    if isinstance(value, int):
        return str(value)
    text = str(value).strip()
    if not text:
        return ""
    if re.fullmatch(r"\d+(?:\.0+)?", text):
        return text.split(".")[0]
    return text


def _raw_or_default(value: Any, fallback: str) -> str:
    """历史记录等路径字段：空值退回默认值，其余原样保留（由 Config 解析成绝对路径）。"""
    if value is None:
        return fallback
    text = str(value).strip()
    return text or fallback


@dataclass
class Config:
    """运行配置。"""

    source: str = "file"
    group_id: str = ""
    days: float = 3.0
    window_mode: str = "days"  # days | auto（auto = 以缓存能覆盖的完整时段为准）
    pool: str = "active"  # active = 活跃群友（按发言加权）｜ all_members = 全群成员（每人等概率）
    # ---- 本地消息存档（长期累积历史，避免被 NapCat 的条数上限挤掉）----
    archive_enabled: bool = True
    archive_path: str = "data/archive.db"
    archive_interval: int = 300
    archive_auto_sync: bool = True
    winners: int = 1
    weighting: bool = True
    weight_cap: int = 0
    min_messages: int = 1
    exclude_roles: list[str] = field(default_factory=list)
    blacklist: list[str] = field(default_factory=list)
    whitelist: list[str] = field(default_factory=list)
    seed: int | None = None
    post_to_group: bool = False
    history_file: str = "data/history.jsonl"
    file_path: str = "data/messages.json"
    file_format: str = "auto"
    onebot_base_url: str = "http://127.0.0.1:3000"
    onebot_access_token: str = ""
    onebot_fetch_limit: int = 30000
    onebot_page_size: int = 1500
    onebot_max_pages: int = 300
    onebot_exclude_self: bool = True
    config_path: str = ""

    # ---- 构造 ----
    @classmethod
    def load(cls, path: str | Path | None = None, overrides: dict[str, Any] | None = None) -> "Config":
        data: dict[str, Any] = {}
        used_path = ""
        base = paths.project_root()
        candidate = paths.resolve(path, base) if path else base / DEFAULT_CONFIG_NAME
        if candidate.is_file():
            try:
                data = json.loads(candidate.read_text(encoding="utf-8-sig"))
            except json.JSONDecodeError as exc:
                raise ConfigError(f"配置文件 {candidate} 不是合法 JSON：{exc}") from exc
            if not isinstance(data, dict):
                raise ConfigError(f"配置文件 {candidate} 顶层必须是对象")
            used_path = str(candidate)
        elif path:
            raise ConfigError(f"配置文件不存在：{candidate}")

        merged = _deep_merge(DEFAULTS, data)
        if overrides:
            merged = _deep_merge(merged, {k: v for k, v in overrides.items() if v is not None})

        file_cfg = merged.get("file") or {}
        onebot_cfg = merged.get("onebot") or {}
        archive_cfg = merged.get("archive") or {}

        source = str(merged.get("source") or "file").lower()
        if source not in ("file", "onebot"):
            raise ConfigError(f"source 只能是 file 或 onebot，当前为 {source!r}")

        window_mode = str(merged.get("window_mode") or "days").lower()
        if window_mode not in ("days", "auto"):
            raise ConfigError(f"window_mode 只能是 days 或 auto，当前为 {window_mode!r}")

        pool = str(merged.get("pool") or "active").lower()
        if pool not in ("active", "all_members"):
            raise ConfigError(f"pool 只能是 active 或 all_members，当前为 {pool!r}")

        cfg = cls(
            source=source,
            group_id=to_user_id(merged.get("group_id")),
            days=float(merged.get("days") or 3),
            window_mode=window_mode,
            pool=pool,
            archive_enabled=bool(archive_cfg.get("enabled", True)),
            archive_path=_raw_or_default(archive_cfg.get("path"), "data/archive.db"),
            archive_interval=max(30, int(archive_cfg.get("sync_interval_seconds") or 300)),
            archive_auto_sync=bool(archive_cfg.get("auto_sync", True)),
            winners=int(merged.get("winners") or 1),
            weighting=bool(merged.get("weighting", True)),
            weight_cap=int(merged.get("weight_cap") or 0),
            min_messages=int(merged.get("min_messages") or 1),
            exclude_roles=[str(r).lower() for r in (merged.get("exclude_roles") or [])],
            blacklist=[to_user_id(x) for x in (merged.get("blacklist") or []) if to_user_id(x)],
            whitelist=[to_user_id(x) for x in (merged.get("whitelist") or []) if to_user_id(x)],
            seed=merged.get("seed"),
            post_to_group=bool(merged.get("post_to_group", False)),
            history_file=_raw_or_default(merged.get("history_file"), "data/history.jsonl"),
            file_path=_raw_or_default(file_cfg.get("path"), "data/messages.json"),
            file_format=str(file_cfg.get("format") or "auto").lower(),
            onebot_base_url=str(onebot_cfg.get("base_url") or "http://127.0.0.1:3000"),
            onebot_access_token=str(onebot_cfg.get("access_token") or ""),
            onebot_fetch_limit=int(onebot_cfg.get("fetch_limit") or 30000),
            onebot_page_size=int(onebot_cfg.get("page_size") or 1500),
            onebot_max_pages=int(onebot_cfg.get("max_pages") or 300),
            onebot_exclude_self=bool(onebot_cfg.get("exclude_self", True)),
            config_path=used_path,
        )
        cfg.validate()
        return cfg

    def validate(self) -> None:
        if self.winners < 1:
            raise ConfigError("winners 必须 >= 1")
        if self.days <= 0:
            raise ConfigError("days 必须 > 0")
        if self.min_messages < 1:
            raise ConfigError("min_messages 必须 >= 1")
        if self.weight_cap < 0:
            raise ConfigError("weight_cap 不能为负数")
        # 注意：这里**不**强制要求 group_id。
        # 界面需要在"还没选群"的状态下也能读配置、列群列表，
        # 群号缺失只在真正要拉消息/发消息时才算错误（由对应流程单独报错）。

    # ---- 便捷属性 ----
    @property
    def window_seconds(self) -> float:
        # auto 模式：以缓存里最早一条消息为起点，窗口取一个足够大的值
        if self.window_mode == "auto":
            return 3650 * 86400.0
        return self.days * 86400.0

    def window_start(self, reference: float) -> float:
        return reference - self.window_seconds

    def describe(self) -> str:
        if self.pool == "all_members":
            return f"全群成员（每人等概率） · 抽取 {self.winners} 人"
        mode = "按发言条数加权" if self.weighting else "每人等概率"
        scope = "缓存覆盖的完整时段" if self.window_mode == "auto" else f"近 {self.days:g} 天"
        return f"{scope}内有发言的群友 · {mode} · 抽取 {self.winners} 人"

    # ---- 路径 ----
    @property
    def file_path_abs(self) -> Path:
        """离线记录文件的绝对路径（相对程序目录解析）。"""
        return paths.resolve(self.file_path)

    @property
    def archive_path_abs(self) -> Path:
        """消息存档数据库的绝对路径（相对程序目录解析）。"""
        return paths.resolve(self.archive_path)

    @property
    def history_file_abs(self) -> Path | None:
        if not self.history_file:
            return None
        return paths.resolve(self.history_file)

    @property
    def config_file_abs(self) -> Path:
        if self.config_path:
            return Path(self.config_path)
        return paths.project_root() / DEFAULT_CONFIG_NAME

    # ---- 持久化 ----
    def to_file_dict(self) -> dict[str, Any]:
        """转成写入 config.json 的结构（不含界面专用的说明键）。"""
        data: dict[str, Any] = {
            "_说明": "由界面或命令行生成。以 _ 开头的键会被程序忽略，可自行加注释。",
            "source": self.source,
            "group_id": self.group_id,
            "days": self.days,
            "window_mode": self.window_mode,
            "pool": self.pool,
            "archive": {
                "enabled": self.archive_enabled,
                "path": paths.resolve(self.archive_path).relative_to(paths.project_root()).as_posix(),
                "sync_interval_seconds": self.archive_interval,
                "auto_sync": self.archive_auto_sync,
            },
            "winners": self.winners,
            "weighting": self.weighting,
            "weight_cap": self.weight_cap,
            "min_messages": self.min_messages,
            "exclude_roles": list(self.exclude_roles),
            "blacklist": list(self.blacklist),
            "whitelist": list(self.whitelist),
            "seed": self.seed,
            "post_to_group": self.post_to_group,
            "history_file": paths.display(self.history_file) if self.history_file else "",
            "file": {
                "path": paths.display(self.file_path),
                "format": self.file_format,
            },
            "onebot": {
                "base_url": self.onebot_base_url,
                "access_token": self.onebot_access_token,
                "fetch_limit": self.onebot_fetch_limit,
                "page_size": self.onebot_page_size,
                "max_pages": self.onebot_max_pages,
                "exclude_self": self.onebot_exclude_self,
            },
        }
        return data

    def save(self, path: str | Path | None = None) -> Path:
        target = paths.resolve(path) if path else self.config_file_abs
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps(self.to_file_dict(), ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )
        self.config_path = str(target)
        return target

    # ---- 变异辅助 ----
    def with_extra_blacklist(self, extra: list[str]) -> "Config":
        merged = list(dict.fromkeys(self.blacklist + [to_user_id(x) for x in extra if to_user_id(x)]))
        self.blacklist = merged
        return self
