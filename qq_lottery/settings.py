"""界面的配置持久化。

界面读写的就是同一个 config.json，所以命令行与图形界面天然共享设置。
"""

from __future__ import annotations

import json
import os
import tempfile
from pathlib import Path
from typing import Any

from . import paths
from .config import DEFAULT_CONFIG_NAME, Config, ConfigError, to_user_id


class ConfigFile:
    """config.json 的读写与校验。"""

    def __init__(self, path: str | Path | None = None) -> None:
        self.path: Path = paths.resolve(path) if path else paths.project_root() / DEFAULT_CONFIG_NAME
        self.data: dict[str, Any] = {}

    # ---- 读取 ----
    def load(self) -> dict[str, Any]:
        if not self.path.is_file():
            # 首次运行：用默认配置落盘，保证界面和命令行看到同一份设置
            cfg = Config()
            self.data = cfg.to_file_dict()
            self.save(self.data)
            return self.data
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8-sig"))
        except json.JSONDecodeError as exc:
            raise ConfigError(f"config.json 不是合法 JSON：{exc}") from exc
        if not isinstance(raw, dict):
            raise ConfigError("config.json 顶层必须是对象")
        self.data = raw
        return raw

    def to_config(self) -> Config:
        """把当前配置转成运行时 Config 对象。"""
        return Config.load(self.path)

    # ---- 写入 ----
    def save(self, data: dict[str, Any]) -> Path:
        """原子写入，避免中途失败留下半个文件。"""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = self._sanitize(data)
        text = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
        fd, tmp_name = tempfile.mkstemp(dir=str(self.path.parent), prefix=".config-", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
                fh.write(text)
            os.replace(tmp_name, self.path)
        except BaseException:
            Path(tmp_name).unlink(missing_ok=True)
            raise
        self.data = payload
        return self.path

    @staticmethod
    def _sanitize(data: dict[str, Any]) -> dict[str, Any]:
        """只保留程序认识的字段，去掉界面传来的临时键。"""
        out: dict[str, Any] = {"_说明": "由界面或命令行生成。以 _ 开头的键会被程序忽略，可自行加注释。"}
        simple = (
            "source",
            "group_id",
            "days",
            "window_mode",
            "pool",
            "winners",
            "weighting",
            "weight_cap",
            "min_messages",
            "exclude_roles",
            "blacklist",
            "whitelist",
            "seed",
            "post_to_group",
            "history_file",
            "archive",
        )
        for key in simple:
            if key in data:
                out[key] = data[key]
        file_cfg = data.get("file") or {}
        out["file"] = {
            "path": str(file_cfg.get("path") or "data/messages.json"),
            "format": str(file_cfg.get("format") or "auto"),
        }
        archive = data.get("archive") or {}
        onebot = data.get("onebot") or {}
        out["archive"] = {
            "enabled": bool(archive.get("enabled", True)),
            "path": str(archive.get("path") or "data/archive.db"),
            "sync_interval_seconds": int(archive.get("sync_interval_seconds") or 300),
            "auto_sync": bool(archive.get("auto_sync", True)),
        }
        out["onebot"] = {
            "base_url": str(onebot.get("base_url") or "http://127.0.0.1:3000"),
            "access_token": str(onebot.get("access_token") or ""),
            "fetch_limit": int(onebot.get("fetch_limit") or 30000),
            "page_size": int(onebot.get("page_size") or 200),
            "max_pages": int(onebot.get("max_pages") or 300),
            "exclude_self": bool(onebot.get("exclude_self", True)),
        }
        return out

    def merge(self, patch: dict[str, Any]) -> Path:
        """把界面上来的局部改动合并进**磁盘上的真实配置**后再保存。

        为什么要先读盘：save() 会为缺失字段填默认值，如果只拿内存里的旧数据
        做基底，用户没提交的字段（尤其是 onebot.base_url、access_token）会被
        默认值悄悄覆盖——那会导致配置指向错误的机器人地址。
        """
        base: dict[str, Any] = {}
        if self.path.is_file():
            try:
                loaded = json.loads(self.path.read_text(encoding="utf-8-sig"))
                if isinstance(loaded, dict):
                    base = loaded
            except (json.JSONDecodeError, OSError):
                base = dict(self.data or {})
        else:
            base = dict(self.data or {})

        merged = dict(base)
        for key, value in patch.items():
            if key in ("file", "onebot") and isinstance(value, dict):
                current = merged.get(key)
                merged[key] = {**(current if isinstance(current, dict) else {}), **value}
            else:
                merged[key] = value
        return self.save(merged)

    # ---- 界面用视图 ----
    def view(self) -> dict[str, Any]:
        """给界面用的扁平结构（带默认值兜底）。"""
        d = self.data or {}
        file_cfg = d.get("file") or {}
        onebot = d.get("onebot") or {}
        archive = d.get("archive") or {}
        return {
            "source": str(d.get("source") or "file"),
            "group_id": to_user_id(d.get("group_id")),
            "days": float(d.get("days") or 3),
            "window_mode": str(d.get("window_mode") or "days"),
            "pool": str(d.get("pool") or "active"),
            "winners": int(d.get("winners") or 1),
            "weighting": bool(d.get("weighting", True)),
            "weight_cap": int(d.get("weight_cap") or 0),
            "min_messages": int(d.get("min_messages") or 1),
            "exclude_roles": [str(r).lower() for r in (d.get("exclude_roles") or [])],
            "blacklist": [to_user_id(x) for x in (d.get("blacklist") or []) if to_user_id(x)],
            "whitelist": [to_user_id(x) for x in (d.get("whitelist") or []) if to_user_id(x)],
            "seed": d.get("seed"),
            "post_to_group": bool(d.get("post_to_group", False)),
            "history_file": str(d.get("history_file") or "data/history.jsonl"),
            "file": {
                "path": str(file_cfg.get("path") or "data/messages.json"),
                "format": str(file_cfg.get("format") or "auto"),
            },
            "archive": {
                "enabled": bool(archive.get("enabled", True)),
                "path": str(archive.get("path") or "data/archive.db"),
                "sync_interval_seconds": int(archive.get("sync_interval_seconds") or 300),
                "auto_sync": bool(archive.get("auto_sync", True)),
            },
            "onebot": {
                "base_url": str(onebot.get("base_url") or "http://127.0.0.1:3000"),
                "access_token": str(onebot.get("access_token") or ""),
                "fetch_limit": int(onebot.get("fetch_limit") or 30000),
                "page_size": int(onebot.get("page_size") or 200),
                "max_pages": int(onebot.get("max_pages") or 300),
                "exclude_self": bool(onebot.get("exclude_self", True)),
            },
            "config_path": paths.display(self.path),
            "root": str(paths.project_root()),
            # 界面据此提示"当前环境禁止发送"（测试环境会置位）
            "send_disabled": _sending_blocked(),
        }


def _sending_blocked() -> bool:
    from .sources import sending_blocked

    return sending_blocked()
