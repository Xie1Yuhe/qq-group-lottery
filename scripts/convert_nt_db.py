#!/usr/bin/env python3
"""把「已解密的 QQ NT 消息库」转成抽奖工具能直接读的数据文件。

背景：QQ NT 的 nt_msg.db 默认加密，本脚本**不负责解密**，
只处理已经解密成普通 SQLite 的库（例如通过你自选的解密工具处理后的结果）。

用法：
    # 1) 先看库里有什么（表名、群号、消息量、时间范围），不写任何文件
    python scripts/convert_nt_db.py --db-dir "D:\\解密后的目录" --list

    # 2) 导出某个群的全部消息
    python scripts/convert_nt_db.py --db-dir "D:\\解密后的目录" --group 123456789 -o data/messages.json

    # 3) 只导出最近 7 天，顺带产出 JSONL
    python scripts/convert_nt_db.py --db-dir "..." --group 123456789 --days 7 \
        -o data/messages.json --jsonl data/messages.jsonl

输出格式与 tools 的离线数据源一致（每行含 group_id / user_id / nickname / time / content），
可以直接：python run_lottery.py draw --source file --file data/messages.json
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Iterator

# ---------------------------------------------------------------- protobuf 解析

WIRE_VARINT = 0
WIRE_64BIT = 1
WIRE_LENGTH = 2
WIRE_32BIT = 5


def iter_fields(blob: bytes) -> Iterator[tuple[int, int, Any]]:
    """极简 protobuf wire 解析：逐个吐出 (字段号, wire 类型, 值)。

    不需要 .proto 定义，也就能在腾讯改字段编号时保持一定的容错。
    """
    i = 0
    n = len(blob)
    while i < n:
        # 读 tag
        tag = 0
        shift = 0
        while i < n:
            b = blob[i]
            i += 1
            tag |= (b & 0x7F) << shift
            shift += 7
            if not b & 0x80:
                break
            if shift > 63:
                return
        if tag == 0:
            return
        field = tag >> 3
        wire = tag & 0x07

        if wire == WIRE_VARINT:
            value = 0
            shift = 0
            while i < n:
                b = blob[i]
                i += 1
                value |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            yield field, wire, value
        elif wire == WIRE_LENGTH:
            length = 0
            shift = 0
            while i < n:
                b = blob[i]
                i += 1
                length |= (b & 0x7F) << shift
                shift += 7
                if not b & 0x80:
                    break
            chunk = blob[i : i + length]
            i += length
            yield field, wire, chunk
        elif wire == WIRE_64BIT:
            chunk = blob[i : i + 8]
            i += 8
            yield field, wire, chunk
        elif wire == WIRE_32BIT:
            chunk = blob[i : i + 4]
            i += 4
            yield field, wire, chunk
        else:
            return  # 未知 wire 类型，放弃剩余部分


def _looks_like_text(data: bytes) -> bool:
    if not data or len(data) < 1:
        return False
    if b"\x00" in data:
        return False
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return False
    printable = sum(1 for ch in text if ch.isprintable() or ch in "\n\t")
    return printable / max(len(text), 1) > 0.85


def extract_content(blob: bytes | None) -> str:
    """从消息体里抽出可读文本。

    QQ NT 把消息正文放在 40800 列的 protobuf 里；正文本身又是嵌套结构
    （常见为 47602 -> 47603 的文本，或 47602 下的其他元素）。
    这里做启发式提取：优先取嵌套里的最长可读文本。
    """
    if not blob:
        return ""
    if isinstance(blob, str):
        return blob

    texts: list[str] = []

    def walk(data: bytes, depth: int = 0) -> None:
        if depth > 6:
            return
        if _looks_like_text(data):
            texts.append(data.decode("utf-8"))
            return
        for _field, wire, value in iter_fields(data):
            if wire == WIRE_LENGTH and isinstance(value, (bytes, bytearray)):
                walk(bytes(value), depth + 1)

    walk(blob)

    cleaned = []
    for text in texts:
        text = text.strip()
        # 过滤掉纯控制/结构性的短串
        if len(text) >= 1 and any(ch.isalnum() or "\u4e00" <= ch <= "\u9fff" for ch in text):
            cleaned.append(text)
    if not cleaned:
        return ""
    return max(cleaned, key=len)


# ---------------------------------------------------------------- 库探查


def open_db(path: Path) -> sqlite3.Connection:
    con = sqlite3.connect(f"file:{path.as_posix()}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    return con


def check_plaintext(path: Path) -> tuple[bool, str]:
    """确认是不是已解密的普通 SQLite 库。"""
    try:
        with path.open("rb") as fh:
            head = fh.read(16)
    except OSError as exc:
        return False, f"无法读取：{exc}"
    if head.startswith(b"SQLite format 3"):
        return True, "已解密（标准 SQLite）"
    if head.startswith(b"SQLite header 3"):
        return False, "仍是加密状态（QQ NT 专有格式），需要先自行解密"
    return False, f"不是 SQLite 库（开头：{head[:12]!r}）"


def find_msg_db(db_dir: Path) -> Path:
    for name in ("nt_msg.db", "nt_msg.decrypt", "nt_msg_decrypted.db"):
        candidate = db_dir / name
        if candidate.is_file():
            return candidate
    candidates = sorted(p for p in db_dir.glob("*.db") if p.stat().st_size > 1024)
    if not candidates:
        raise SystemExit(f"在 {db_dir} 里找不到 .db 文件")
    return max(candidates, key=lambda p: p.stat().st_size)


def list_tables(con: sqlite3.Connection) -> list[str]:
    return [r[0] for r in con.execute("select name from sqlite_master where type='table' order by name")]


def pick_table(tables: Iterable[str], kind: str) -> str | None:
    names = list(tables)
    if kind == "group":
        for t in names:
            if "group" in t.lower() and "msg" in t.lower() and "fts" not in t.lower():
                return t
        for t in names:
            if t.lower().startswith("group"):
                return t
    else:
        for t in names:
            if "c2c" in t.lower() and "msg" in t.lower():
                return t
    for t in names:
        if "msg" in t.lower() and "fts" not in t.lower():
            return t
    return None


def table_columns(con: sqlite3.Connection, table: str) -> list[str]:
    return [r[1] for r in con.execute(f'PRAGMA table_info("{table}")')]


def pick_column(columns: list[str], *keywords: str) -> str | None:
    lowered = {c.lower(): c for c in columns}
    for want in keywords:
        if want.lower() in lowered:
            return lowered[want.lower()]
    for want in keywords:
        for low, original in lowered.items():
            if want.lower() in low:
                return original
    return None


# ---------------------------------------------------------------- 导出


def row_to_record(con: sqlite3.Connection, table: str, row: sqlite3.Row, columns: list[str]) -> dict[str, Any] | None:
    uid_col = pick_column(columns, "40020")
    time_col = pick_column(columns, "40050", "time")
    body_col = pick_column(columns, "40800")
    peer_col = pick_column(columns, "40021", "40027", "peer")

    if uid_col is None or time_col is None:
        return None

    try:
        ts = int(row[time_col])
    except (TypeError, ValueError):
        return None
    if ts > 1e11:
        ts //= 1000

    uid = row[uid_col]
    uid = str(uid) if uid is not None else ""

    text = extract_content(row[body_col]) if body_col else ""
    if not text:
        return None

    peer = row[peer_col] if peer_col else ""
    return {
        "group_id": str(peer) if peer is not None else "",
        "user_id": uid,
        "nickname": uid,
        "time": ts,
        "content": text,
    }


def export(
    db_dir: Path,
    group: str | None,
    days: float | None,
    limit: int | None,
    out: Path,
    jsonl: Path | None,
) -> int:
    msg_db = find_msg_db(db_dir)
    ok, reason = check_plaintext(msg_db)
    print(f"消息库: {msg_db}（{msg_db.stat().st_size:,} 字节）")
    print(f"状态  : {reason}")
    if not ok:
        return 2

    con = open_db(msg_db)
    tables = list_tables(con)
    print(f"表    : {', '.join(tables[:12])}{' …' if len(tables) > 12 else ''}")

    table = pick_table(tables, "group")
    if not table:
        print("找不到消息表", file=sys.stderr)
        return 3
    columns = table_columns(con, table)
    print(f"使用表: {table}（{len(columns)} 列）")

    uid_col = pick_column(columns, "40020")
    time_col = pick_column(columns, "40050", "time")
    body_col = pick_column(columns, "40800")
    peer_col = pick_column(columns, "40021", "40027")
    print(f"字段  : user={uid_col} time={time_col} body={body_col} peer={peer_col}")
    if not uid_col or not time_col or not body_col:
        print("字段识别失败，无法导出", file=sys.stderr)
        return 4

    sql = f'SELECT * FROM "{table}"'
    params: list[Any] = []
    if group and peer_col:
        sql += f' WHERE "{peer_col}" = ?'
        params.append(int(group) if str(group).isdigit() else group)
    if days:
        sql += (" AND " if "WHERE" in sql else " WHERE ") + f'"{time_col}" >= ?'
        params.append(int((time.time() - days * 86400) * 1000))
    sql += f' ORDER BY "{time_col}" ASC'
    if limit:
        sql += f" LIMIT {int(limit)}"

    records: list[dict[str, Any]] = []
    skipped = 0
    for row in con.execute(sql, params):
        rec = row_to_record(con, table, row, columns)
        if rec:
            records.append(rec)
        else:
            skipped += 1
    con.close()

    if not records:
        print("没有解析出任何消息（可能是筛选条件不对，或消息体为纯图片/表情）", file=sys.stderr)
        return 5

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2), encoding="utf-8")
    if jsonl:
        jsonl.parent.mkdir(parents=True, exist_ok=True)
        with jsonl.open("w", encoding="utf-8") as fh:
            for rec in records:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    first = datetime.fromtimestamp(min(r["time"] for r in records))
    last = datetime.fromtimestamp(max(r["time"] for r in records))
    users = {r["user_id"] for r in records}
    print()
    print(f"导出完成：{len(records):,} 条消息（跳过 {skipped:,} 条无文本消息）")
    print(f"  时间范围: {first:%Y-%m-%d %H:%M} ~ {last:%Y-%m-%d %H:%M}")
    print(f"  发言人数: {len(users)}")
    print(f"  输出文件: {out}" + (f" 与 {jsonl}" if jsonl else ""))
    print()
    print("接着执行：")
    print(f'  python run_lottery.py stats --source file --file "{out}" --top 30')
    print(f'  python run_lottery.py draw  --source file --file "{out}" -n 3')
    return 0


def inspect(db_dir: Path) -> int:
    msg_db = find_msg_db(db_dir)
    ok, reason = check_plaintext(msg_db)
    print(f"消息库: {msg_db}（{msg_db.stat().st_size:,} 字节）")
    print(f"状态  : {reason}")
    if not ok:
        print("\n这个库还没有解密。本项目不提供解密功能，请先用你信任的工具把库解密成普通 SQLite 再回来。")
        return 2

    con = open_db(msg_db)
    for table in list_tables(con):
        try:
            count = con.execute(f'SELECT COUNT(*) FROM "{table}"').fetchone()[0]
        except sqlite3.Error:
            continue
        if count == 0:
            continue
        columns = table_columns(con, table)
        print(f"\n表 {table}：{count:,} 行，{len(columns)} 列")
        print(f"  列名: {', '.join(columns[:16])}{' …' if len(columns) > 16 else ''}")

        peer_col = pick_column(columns, "40021", "40027")
        time_col = pick_column(columns, "40050", "time")
        if peer_col and time_col:
            print("  会话与时间分布（前 15 个）：")
            try:
                rows = con.execute(
                    f'SELECT "{peer_col}" AS peer, COUNT(*) AS n, MIN("{time_col}") AS t0, MAX("{time_col}") AS t1 '
                    f'FROM "{table}" GROUP BY "{peer_col}" ORDER BY n DESC LIMIT 15'
                ).fetchall()
                for row in rows:
                    t0 = datetime.fromtimestamp(row["t0"] / (1000 if row["t0"] > 1e11 else 1))
                    t1 = datetime.fromtimestamp(row["t1"] / (1000 if row["t1"] > 1e11 else 1))
                    print(f"    {row['peer']:<16} {row['n']:>9,} 条   {t0:%Y-%m-%d} ~ {t1:%Y-%m-%d}")
            except sqlite3.Error as exc:
                print(f"    统计失败：{exc}")
    con.close()
    print("\n选出你要导出的群号后，执行：")
    print(f'  python scripts/convert_nt_db.py --db-dir "{db_dir}" --group <群号> -o data/messages.json')
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="把已解密的 QQ NT 消息库转成抽奖工具可读的数据文件（不含解密功能）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--db-dir", required=True, help="包含已解密 nt_msg.db 的目录")
    parser.add_argument("--group", help="只导出这个群号")
    parser.add_argument("--days", type=float, help="只导出最近 N 天")
    parser.add_argument("--limit", type=int, help="最多导出多少条")
    parser.add_argument("-o", "--out", default="data/messages.json", help="输出 JSON 路径")
    parser.add_argument("--jsonl", help="额外输出 JSONL 路径")
    parser.add_argument("--list", action="store_true", help="只列出库里的会话与消息量，不导出")
    args = parser.parse_args(argv)

    db_dir = Path(args.db_dir).expanduser()
    if not db_dir.is_dir():
        print(f"目录不存在：{db_dir}", file=sys.stderr)
        return 2

    try:
        if args.list:
            return inspect(db_dir)
        return export(
            db_dir,
            args.group,
            args.days,
            args.limit,
            Path(args.out),
            Path(args.jsonl) if args.jsonl else None,
        )
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())
