#!/usr/bin/env python3
"""生成演示用的假聊天记录，方便先试用抽奖效果。

    python scripts/make_demo_data.py

会在 data/ 下生成 demo_messages.json、demo_messages.jsonl、demo_messages.csv。
"""

from __future__ import annotations

import csv
import json
import random
import sys
import time
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"

GROUP = "123456789"

# (QQ号, 昵称, 近三天发言条数, 最后一次发言距今小时数)
MEMBERS = [
    ("10001", "夜航船", 62, 0.2),
    ("10002", "薄荷汽水", 41, 1.1),
    ("10003", "阿柴不吃鱼", 33, 0.6),
    ("10004", "半糖去冰", 27, 2.4),
    ("10005", "风吹麦浪", 19, 3.0),
    ("10006", "凉生", 14, 5.5),
    ("10007", "小满", 11, 8.0),
    ("10008", "程序猿甲", 9, 12.5),
    ("10009", "拿铁不加糖", 7, 20.0),
    ("10010", "橙子味夏天", 5, 26.0),
    ("10011", "像素猫", 4, 38.0),
    ("10012", "白日梦想家", 3, 47.0),
    ("10013", "一只鹿", 2, 60.0),
    ("10014", "路人乙", 1, 70.5),
    # 近三天完全没说话的潜水员（不应该进候选）
    ("10015", "三天前的我", 0, 96.0),
    ("10016", "失踪人口", 0, 240.0),
    # 机器人账号
    ("2854196310", "QQ机器人", 120, 0.1),
]

WORDS = [
    "晚上好", "这题我会", "哈哈哈哈哈", "已阅", "+1", "发个表情包", "在的", "干活了",
    "摸鱼中", "有人吗", "求个资源", "笑死", "确实", "太难了", "打卡", "早安", "晚安",
    "吃了吗", "图我收下了", "明天见",
]


def build_messages() -> list[dict]:
    rng = random.Random(20240520)
    now = time.time()
    messages: list[dict] = []
    mid = 100000
    for uid, nick, count, last_hours in MEMBERS:
        if count <= 0:
            # 潜水员：所有消息都落在 4~10 天前，不进入三天窗口
            for _ in range(rng.randint(1, 4)):
                ts = now - rng.uniform(4, 10) * 86400
                mid += 1
                messages.append(_msg(uid, nick, ts, mid, rng))
            continue
        # 该成员最早一条消息距今 span_h 小时，最后一条距今 last_hours 小时
        span_h = last_hours + min(60.0, 6.0 + count * 1.3)
        for i in range(count):
            # 时间从最早线性推进到最近，再叠加小幅抖动，分布自然
            base = span_h + (last_hours - span_h) * (i / max(count - 1, 1))
            jitter = rng.uniform(-0.6, 0.6) * (span_h - last_hours) / max(count, 1)
            offset_hours = min(max(base + jitter, last_hours), span_h)
            ts = now - offset_hours * 3600
            mid += 1
            messages.append(_msg(uid, nick, ts, mid, rng))
    messages.sort(key=lambda m: m["time"])
    return messages


def _msg(uid: str, nick: str, ts: float, mid: int, rng: random.Random) -> dict:
    return {
        "self_id": 3000000001,
        "post_type": "message",
        "message_type": "group",
        "group_id": int(GROUP),
        "message_id": mid,
        "message_seq": mid,
        "user_id": int(uid),
        "time": int(ts),
        "raw_message": rng.choice(WORDS),
        "sender": {
            "user_id": int(uid),
            "nickname": nick,
            "card": nick,
            "role": "admin" if uid == "10004" else ("owner" if uid == "10001" else "member"),
        },
    }


def main() -> int:
    DATA.mkdir(parents=True, exist_ok=True)
    messages = build_messages()

    # data/messages.json 是 config.json 默认读取的文件，放一份演示数据让工具开箱可用。
    # 换成你自己的聊天记录时，直接覆盖这个文件即可。
    json_path = DATA / "messages.json"
    json_text = json.dumps(messages, ensure_ascii=False, indent=2)
    json_path.write_text(json_text, encoding="utf-8")

    # 另外三种格式的样例，标题写明是演示数据，避免和真实记录混淆
    sample_json = DATA / "sample_messages.json"
    sample_json.write_text(
        json.dumps(
            {
                "_说明": "演示用的假聊天记录（OneBot get_group_msg_history 风格）",
                "_生成脚本": "scripts/make_demo_data.py",
                "status": "ok",
                "retcode": 0,
                "data": {"messages": messages},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    jsonl_path = DATA / "sample_messages.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as fh:
        for row in messages:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    csv_path = DATA / "sample_messages.csv"
    with csv_path.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["time", "user_id", "nickname", "role", "group_id", "content"])
        for row in messages:
            writer.writerow(
                [
                    datetime.fromtimestamp(row["time"]).strftime("%Y-%m-%d %H:%M:%S"),
                    row["user_id"],
                    row["sender"]["nickname"],
                    row["sender"]["role"],
                    row["group_id"],
                    row["raw_message"],
                ]
            )

    txt_path = DATA / "sample_messages.txt"
    with txt_path.open("w", encoding="utf-8") as fh:
        last_day = ""
        for row in messages:
            dt = datetime.fromtimestamp(row["time"])
            day = dt.strftime("%Y-%m-%d")
            if day != last_day:
                fh.write(f"===== {day} =====\n")
                last_day = day
            fh.write(f"{dt:%H:%M:%S} {row['sender']['nickname']}({row['user_id']}): {row['raw_message']}\n")

    print(f"已生成演示数据（共 {len(messages)} 条消息）：")
    for path in (json_path, sample_json, jsonl_path, csv_path, txt_path):
        print(f"  - {path.relative_to(ROOT)}")
    print("提示：把 data/messages.json 换成你自己的聊天记录即可正式使用。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
