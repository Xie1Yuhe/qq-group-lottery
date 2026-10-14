"""本地消息存档：把机器人拉到的消息增量落盘到 SQLite，长期累积成完整历史。

为什么需要它
------------
NapCat/QQ 的本地消息缓存上限是**固定条数**（本机实测约 1628 条，本群约 80 分钟），
所以「近三天」在活跃群里拿不到完整数据——旧消息会被新消息挤掉。

存档的做法：每隔一段时间把机器人当前能捞到的消息全部 upsert 进本地 SQLite。
于是**只要工具在运行，历史就不会丢**，时间窗口越长攒得越完整。

能力的边界（必须如实告知使用者）
--------------------------------
  · 只能补「工具在线期间」收到的消息；
  · 机器人/工具离线期间的空洞**无法恢复**（那是 QQ 服务端的限制）；
  · 所以它让「窗口完整」成为可能，但不是凭空变出历史。
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Sequence

from . import paths
from .model import Message

SCHEMA = """
CREATE TABLE IF NOT EXISTS messages (
    key         TEXT PRIMARY KEY,   -- 稳定去重键
    message_id  TEXT,
    group_id    TEXT,
    user_id     TEXT NOT NULL,
    nickname    TEXT,
    role        TEXT,
    ts          REAL NOT NULL,      -- 本地时间戳（秒）
    raw         TEXT,               -- 原始 raw_message，仅本地保存
    archived_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_group_ts ON messages(group_id, ts);
CREATE INDEX IF NOT EXISTS idx_msg_user_ts  ON messages(user_id, ts);

CREATE TABLE IF NOT EXISTS sync_state (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE TABLE IF NOT EXISTS sync_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    started_at  REAL NOT NULL,
    finished_at REAL,
    fetched     INTEGER DEFAULT 0,
    inserted    INTEGER DEFAULT 0,
    updated     INTEGER DEFAULT 0,
    total_after INTEGER DEFAULT 0,
    ok          INTEGER DEFAULT 1,
    message     TEXT
);
"""


def message_key(msg: Message) -> str:
    """稳定的去重键：优先用消息 ID，没有就用「用户+时间戳」，都与内容无关。"""
    if msg.message_id:
        return f"id:{msg.message_id}"
    return f"ut:{msg.user_id}:{int(msg.timestamp * 1000)}"


def _raw_text(msg: Message) -> str:
    raw = msg.raw or {}
    value = raw.get("raw_message")
    if value is None:
        value = raw.get("content")
    return "" if value is None else str(value)


@dataclass
class ArchiveStatus:
    """存档现状，供界面展示与判断历史深度。"""

    enabled: bool
    path: str
    exists: bool
    total: int
    earliest: float | None
    latest: float | None
    span_hours: float
    groups: list[dict[str, Any]]
    last_sync_at: float | None
    last_sync_result: str
    size_bytes: int

    @property
    def span_label(self) -> str:
        return _human_span(self.span_hours)

    @property
    def size_label(self) -> str:
        return _human_size(self.size_bytes)

    def as_dict(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "path": self.path,
            "exists": self.exists,
            "total": self.total,
            "earliest": (
                datetime.fromtimestamp(self.earliest).strftime("%Y-%m-%d %H:%M") if self.earliest else ""
            ),
            "latest": (
                datetime.fromtimestamp(self.latest).strftime("%Y-%m-%d %H:%M") if self.latest else ""
            ),
            "span_hours": round(self.span_hours, 2),
            "span_label": _human_span(self.span_hours),
            "groups": self.groups,
            "last_sync_at": (
                datetime.fromtimestamp(self.last_sync_at).strftime("%Y-%m-%d %H:%M:%S")
                if self.last_sync_at
                else ""
            ),
            "last_sync_result": self.last_sync_result,
            "size_bytes": self.size_bytes,
            "size_label": _human_size(self.size_bytes),
        }


def _human_span(hours: float) -> str:
    if hours >= 24:
        return f"{hours / 24:.1f} 天"
    if hours >= 1:
        return f"{hours:.1f} 小时"
    return f"{hours * 60:.0f} 分钟"


def _human_size(size: int) -> str:
    for unit, factor in (("GB", 1024**3), ("MB", 1024**2), ("KB", 1024)):
        if size >= factor:
            return f"{size / factor:.1f} {unit}"
    return f"{size} B"


class MessageArchive:
    """SQLite 消息存档。线程安全（内部加锁）。"""

    def __init__(self, path: str | Path, enabled: bool = True) -> None:
        self.path = paths.resolve(path)
        self.enabled = enabled
        self._lock = threading.RLock()
        self._conn: sqlite3.Connection | None = None

    # ---------------------------------------------------------------- 连接
    def _connect(self) -> sqlite3.Connection:
        if self._conn is None:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(str(self.path), timeout=15.0, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            conn.executescript(SCHEMA)
            # WAL 模式：读写并发更友好，避免和界面查询互相阻塞
            try:
                conn.execute("PRAGMA journal_mode=WAL")
                conn.execute("PRAGMA synchronous=NORMAL")
            except sqlite3.Error:
                pass
            conn.commit()
            self._conn = conn
        return self._conn

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                try:
                    self._conn.close()
                finally:
                    self._conn = None

    # ---------------------------------------------------------------- 写入
    def upsert(self, messages: Sequence[Message], group_id: str = "") -> dict[str, int]:
        """把一批消息写入存档，并准确区分「新增」与「已存在」。

        幂等：同一条消息重复写入不会产生重复行（按稳定 key 去重）。
        """
        if not self.enabled:
            return {"fetched": 0, "inserted": 0, "updated": 0}
        rows = []
        now = time.time()
        # 兜底键可能撞车（同一人同一毫秒发两条，且都没 message_id），
        # 这里对批内重复键追加序号，保证唯一。
        seen: dict[str, int] = {}
        for msg in messages:
            key = message_key(msg)
            if key in seen:
                seen[key] += 1
                key = f"{key}#{seen[key]}"
            else:
                seen[key] = 0
            rows.append(
                (
                    key,
                    msg.message_id or "",
                    msg.group_id or group_id or "",
                    msg.user_id,
                    msg.nickname or "",
                    msg.role or "",
                    float(msg.timestamp),
                    _raw_text(msg)[:4000],
                    now,
                )
            )
        if not rows:
            return {"fetched": 0, "inserted": 0, "updated": 0}

        with self._lock:
            conn = self._connect()
            keys = [r[0] for r in rows]
            existing: set[str] = set()
            # 分批查询，避免 SQL 参数过多
            for i in range(0, len(keys), 500):
                chunk = keys[i : i + 500]
                marks = ",".join("?" for _ in chunk)
                existing.update(
                    r[0]
                    for r in conn.execute(f"SELECT key FROM messages WHERE key IN ({marks})", chunk)
                )
            with conn:
                conn.executemany(
                    """
                    INSERT INTO messages
                        (key, message_id, group_id, user_id, nickname, role, ts, raw, archived_at)
                    VALUES (?,?,?,?,?,?,?,?,?)
                    ON CONFLICT(key) DO UPDATE SET
                        nickname = excluded.nickname,
                        role     = excluded.role,
                        group_id = CASE WHEN excluded.group_id <> '' THEN excluded.group_id
                                        ELSE messages.group_id END
                    """,
                    rows,
                )
            inserted = sum(1 for k in keys if k not in existing)
            updated = len(keys) - inserted
        return {"fetched": len(rows), "inserted": inserted, "updated": updated}

    def log_sync(self, started_at: float, result: dict[str, Any], ok: bool = True, message: str = "") -> None:
        if not self.enabled:
            return
        with self._lock:
            conn = self._connect()
            with conn:
                conn.execute(
                    """
                    INSERT INTO sync_log
                        (started_at, finished_at, fetched, inserted, updated, total_after, ok, message)
                    VALUES (?,?,?,?,?,?,?,?)
                    """,
                    (
                        started_at,
                        time.time(),
                        int(result.get("fetched", 0)),
                        int(result.get("inserted", 0)),
                        int(result.get("updated", 0)),
                        int(result.get("total_after", 0)),
                        1 if ok else 0,
                        message[:500],
                    ),
                )

    # ---------------------------------------------------------------- 读取
    def count(self, group_id: str = "") -> int:
        if not self.enabled or not self.path.is_file():
            return 0
        with self._lock:
            conn = self._connect()
            if group_id:
                row = conn.execute(
                    "SELECT COUNT(*) FROM messages WHERE group_id = ? OR group_id = ''", (group_id,)
                ).fetchone()
            else:
                row = conn.execute("SELECT COUNT(*) FROM messages").fetchone()
            return int(row[0]) if row else 0

    def load(self, group_id: str = "", since: float | None = None, until: float | None = None) -> list[Message]:
        """按时间范围读回消息（升序）。"""
        if not self.enabled or not self.path.is_file():
            return []
        sql = "SELECT * FROM messages WHERE 1=1"
        params: list[Any] = []
        if group_id:
            sql += " AND (group_id = ? OR group_id = '')"
            params.append(group_id)
        if since is not None:
            sql += " AND ts >= ?"
            params.append(float(since))
        if until is not None:
            sql += " AND ts <= ?"
            params.append(float(until))
        sql += " ORDER BY ts ASC"

        with self._lock:
            conn = self._connect()
            rows = conn.execute(sql, params).fetchall()
        return [
            Message(
                user_id=str(r["user_id"]),
                nickname=str(r["nickname"] or r["user_id"]),
                timestamp=float(r["ts"]),
                group_id=str(r["group_id"] or ""),
                message_id=str(r["message_id"] or ""),
                role=str(r["role"] or ""),
                raw={"raw_message": r["raw"]} if r["raw"] else {},
            )
            for r in rows
        ]

    def status(self, group_id: str = "") -> ArchiveStatus:
        exists = self.path.is_file()
        size = self.path.stat().st_size if exists else 0
        if not self.enabled:
            return ArchiveStatus(False, paths.display(self.path), exists, 0, None, None, 0.0, [], None, "未启用", size)

        groups: list[dict[str, Any]] = []
        total = 0
        earliest = latest = None
        last_sync_at = None
        last_result = "尚未同步"

        if exists:
            with self._lock:
                conn = self._connect()
                row = conn.execute("SELECT COUNT(*), MIN(ts), MAX(ts) FROM messages").fetchone()
                total = int(row[0] or 0)
                earliest = row[1]
                latest = row[2]
                for r in conn.execute(
                    "SELECT group_id, COUNT(*) AS n, MIN(ts) AS t0, MAX(ts) AS t1 "
                    "FROM messages GROUP BY group_id ORDER BY n DESC LIMIT 20"
                ):
                    groups.append(
                        {
                            "group_id": r["group_id"] or "（未知）",
                            "count": int(r["n"]),
                            "earliest": datetime.fromtimestamp(r["t0"]).strftime("%Y-%m-%d %H:%M"),
                            "latest": datetime.fromtimestamp(r["t1"]).strftime("%Y-%m-%d %H:%M"),
                        }
                    )
                log = conn.execute(
                    "SELECT finished_at, fetched, inserted, updated, ok, message "
                    "FROM sync_log ORDER BY id DESC LIMIT 1"
                ).fetchone()
                if log:
                    last_sync_at = log["finished_at"]
                    if log["ok"]:
                        last_result = (
                            f"上次同步：拉取 {log['fetched']} 条，"
                            f"新增 {log['inserted']}，更新 {log['updated']}"
                        )
                    else:
                        last_result = f"上次同步失败：{log['message'] or '未知原因'}"

        span_hours = ((latest - earliest) / 3600) if (earliest and latest) else 0.0
        return ArchiveStatus(
            True,
            paths.display(self.path),
            exists,
            total,
            earliest,
            latest,
            span_hours,
            groups,
            last_sync_at,
            last_result,
            size,
        )
