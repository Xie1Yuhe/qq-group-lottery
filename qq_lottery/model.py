"""核心数据模型。"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


def now_ts() -> float:
    """当前本地时间戳（秒）。"""
    return time.time()


@dataclass
class Message:
    """一条群消息（只保留抽奖需要的字段）。"""

    user_id: str
    nickname: str
    timestamp: float
    group_id: str = ""
    message_id: str = ""
    role: str = ""  # owner / admin / member
    raw: dict[str, Any] = field(default_factory=dict, repr=False)

    @property
    def dt(self) -> datetime:
        return datetime.fromtimestamp(self.timestamp)


@dataclass
class Activity:
    """单个群友在统计窗口内的活跃度。"""

    user_id: str
    nickname: str
    count: int = 0
    first_ts: float = 0.0
    last_ts: float = 0.0
    weight_input: float = 0.0  # 参与加权计算的原始值
    role: str = ""

    @property
    def last_dt(self) -> datetime:
        return datetime.fromtimestamp(self.last_ts)

    @property
    def first_dt(self) -> datetime:
        return datetime.fromtimestamp(self.first_ts)


@dataclass
class Winner:
    """一位中奖者。"""

    rank: int
    user_id: str
    nickname: str
    count: int
    probability: float  # 该轮抽取时此人的中奖概率（条件概率）
    last_speak: str

    def as_dict(self) -> dict[str, Any]:
        return {
            "rank": self.rank,
            "user_id": self.user_id,
            "nickname": self.nickname,
            "count": self.count,
            "probability": round(self.probability, 6),
            "probability_pct": f"{self.probability * 100:.2f}%",
            "last_speak": self.last_speak,
        }
