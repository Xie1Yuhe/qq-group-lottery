"""消息来源：离线聊天记录文件 + OneBot v11 机器人接口。

对外只暴露两个入口：
  load_from_file(...)  解析 json / jsonl / csv / txt 聊天记录
  load_from_onebot(...) 通过 OneBot v11 HTTP 接口拉取群消息
两者都返回按时间升序排列的 Message 列表。
"""

from __future__ import annotations

import csv
import io
import json
import os
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Iterable, Iterator

from .config import Config, ConfigError, to_user_id
from .model import Message

# ---------------------------------------------------------------- 发送硬锁
#
# 教训：自检脚本曾在用户真实的群配置上运行，把测试开奖文案真的发进了群。
# 因此这里加一道与配置无关的开关：只要环境变量被置位，任何"往群里发消息"
# 的调用都会被直接拒绝。测试脚本必须设置它，这样即便误用了真实配置也发不出去。
#
#   QQLOTTERY_NO_SEND=1   -> 禁止一切发送（拉取/查询不受影响）
SEND_BLOCK_ENV = "QQLOTTERY_NO_SEND"


def sending_blocked() -> bool:
    value = os.environ.get(SEND_BLOCK_ENV, "").strip().lower()
    return value not in ("", "0", "false", "no")


class SendBlockedError(RuntimeError):
    """发送被硬锁拦截。"""


def guard_send(action: str, target: str) -> None:
    if sending_blocked():
        raise SendBlockedError(
            f"发送已被 {SEND_BLOCK_ENV} 禁用，拒绝执行 {action}（目标 {target}）。"
            "这是为了避免测试消息进入真实群。"
        )


# ---------------------------------------------------------------- 字段别名

UID_FIELDS = ("user_id", "sender_id", "qq", "uin", "member_uin", "from_uin", "from_id", "senderUin")
NAME_FIELDS = ("nickname", "card", "sender_name", "name", "nick", "sendername", "member_name", "from_name")
TIME_FIELDS = ("time", "timestamp", "ts", "date", "datetime", "msg_time", "send_time", "created_at")
GROUP_FIELDS = ("group_id", "group", "group_code", "groupcode", "peer_uin")
MID_FIELDS = ("message_id", "msg_id", "msgId", "id", "seq", "message_seq")
ROLE_FIELDS = ("role", "member_role", "permission")

# 已知的腾讯机器人/系统账号
BOT_UIDS = {"2854196310", "2854196306", "2854196311", "2854196312", "2854196313", "2854196314"}
BOT_NAME_HINTS = ("机器人", "bot", "助手", "小冰", "qq安全", "群助手", "管家")

_SYSTEM_MARKERS = ("<", "[", "{")

# ---------------------------------------------------------------- 工具函数


def _pick(row: dict[str, Any], fields: Iterable[str]) -> Any:
    """按别名顺序取值（大小写不敏感）。"""
    if not isinstance(row, dict):
        return None
    lowered = {str(k).lower(): v for k, v in row.items()}
    for name in fields:
        if name in row and row[name] not in (None, ""):
            return row[name]
        value = lowered.get(name.lower())
        if value not in (None, ""):
            return value
    return None


def _norm_key(key: str) -> str:
    return re.sub(r"[\s_\-.]", "", str(key)).lower()


_UID_KEYS = {_norm_key(k) for k in UID_FIELDS}
_NAME_KEYS = {_norm_key(k) for k in NAME_FIELDS}
_TIME_KEYS = {_norm_key(k) for k in TIME_FIELDS}


def parse_timestamp(value: Any) -> float | None:
    """把各种时间写法统一成本地时间戳（秒）。"""
    if value is None or value == "":
        return None
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        num = float(value)
        if num > 1e11:  # 毫秒
            num /= 1000.0
        if num > 1e8:  # 2001 年以后，视为 Unix 时间戳
            return num
        return None
    text = str(value).strip()
    if not text:
        return None
    if re.fullmatch(r"\d{9,13}", text):
        return parse_timestamp(int(text))
    text = text.replace("T", " ").replace("年", "-").replace("月", "-").replace("日", " ")
    text = re.sub(r"(\.\d{3,})\d*", r"\1", text)
    text = re.sub(r"(Z|[+-]\d{2}:?\d{2})$", "", text).strip()
    formats = (
        "%Y-%m-%d %H:%M:%S.%f",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%d %H:%M",
        "%Y-%m-%d",
        "%Y/%m/%d %H:%M:%S",
        "%Y/%m/%d %H:%M",
        "%Y/%m/%d",
        "%m-%d %H:%M:%S",
        "%m-%d %H:%M",
        "%H:%M:%S",
        "%H:%M",
    )
    for fmt in formats:
        try:
            dt = datetime.strptime(text, fmt)
        except ValueError:
            continue
        if fmt.startswith("%m"):
            today = datetime.now()
            dt = dt.replace(year=today.year)
        elif fmt.startswith("%H"):
            today = datetime.now()
            dt = dt.replace(year=today.year, month=today.month, day=today.day)
        return dt.timestamp()
    return None


def row_to_message(row: dict[str, Any], fallback_timestamp: float | None = None) -> Message | None:
    """把一条任意结构的记录转成 Message，取不到 QQ 号就返回 None。"""
    if not isinstance(row, dict):
        return None
    sender = row.get("sender")
    if not isinstance(sender, dict):
        sender = {}

    uid_raw = _pick(row, UID_FIELDS)
    if uid_raw in (None, "") and sender:
        uid_raw = _pick(sender, UID_FIELDS)
    uid = to_user_id(uid_raw)
    if not uid or uid in ("0", "10000", "1000000"):
        return None

    name = _pick(row, NAME_FIELDS)
    if name in (None, "") and sender:
        name = _pick(sender, NAME_FIELDS)
    nickname = str(name).strip() if name not in (None, "") else uid

    ts_raw = _pick(row, TIME_FIELDS)
    if ts_raw in (None, "") and sender:
        ts_raw = _pick(sender, TIME_FIELDS)
    ts = parse_timestamp(ts_raw)
    if ts is None:
        ts = parse_timestamp(row.get("_timestamp"))  # CSV 元数据
    if ts is None:
        ts = fallback_timestamp
    if ts is None:
        return None

    group = to_user_id(_pick(row, GROUP_FIELDS)) or to_user_id(row.get("_group_id"))
    mid = to_user_id(_pick(row, MID_FIELDS))
    role = _pick(row, ROLE_FIELDS)
    if role in (None, "") and sender:
        role = _pick(sender, ROLE_FIELDS)
    return Message(
        user_id=uid,
        nickname=nickname,
        timestamp=ts,
        group_id=group,
        message_id=mid,
        role=str(role).lower() if role not in (None, "") else "",
        raw=row,
    )


# ---------------------------------------------------------------- JSON / JSONL


def _iter_json_records(obj: Any) -> Iterator[dict[str, Any]]:
    """从各种 JSON 结构里挖出消息记录。"""
    if isinstance(obj, list):
        for item in obj:
            yield from _iter_json_records(item)
        return
    if not isinstance(obj, dict):
        return

    looks_like_message = any(_norm_key(k) in _UID_KEYS for k in obj.keys()) and any(
        _norm_key(k) in _TIME_KEYS for k in obj.keys()
    )
    if looks_like_message:
        yield obj
        return

    # 常见包裹层
    for key in ("messages", "data", "msgList", "message", "list", "items", "records", "chat"):
        inner = obj.get(key)
        if isinstance(inner, (list, dict)):
            yield from _iter_json_records(inner)


def load_json(path: Path) -> list[Message]:
    """支持 JSON 数组、OneBot 响应体、JSONL。"""
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    stripped = text.lstrip()
    messages: list[Message] = []

    if stripped.startswith("[") or stripped.startswith("{"):
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            data = None
        if data is not None:
            for row in _iter_json_records(data):
                msg = row_to_message(row)
                if msg:
                    messages.append(msg)
            return messages

    # 回退：逐行 JSON（JSONL）
    for line in text.splitlines():
        line = line.strip().rstrip(",")
        if not line or line in "[]{}":
            continue
        try:
            row = json.loads(line)
        except json.JSONDecodeError:
            continue
        for item in _iter_json_records(row):
            msg = row_to_message(item)
            if msg:
                messages.append(msg)
    return messages


# ---------------------------------------------------------------- CSV / TSV


def load_delimited(path: Path, delimiter: str | None = None) -> list[Message]:
    text = path.read_text(encoding="utf-8-sig", errors="replace")
    if delimiter is None:
        sample = text[:8192]
        try:
            delimiter = csv.Sniffer().sniff(sample, delimiters=",\t;|").delimiter
        except csv.Error:
            delimiter = ","

    messages: list[Message] = []
    reader = csv.reader(io.StringIO(text), delimiter=delimiter)
    try:
        rows = list(reader)
    except csv.Error as exc:
        raise ConfigError(f"解析 {path.name} 失败：{exc}") from exc
    if not rows:
        return messages

    header = [h.strip() for h in rows[0]]
    norm_header = [_norm_key(h) for h in header]
    has_header = any(key in _UID_KEYS for key in norm_header)

    if has_header:
        uid_idx = next((i for i, k in enumerate(norm_header) if k in _UID_KEYS), None)
        name_idx = next((i for i, k in enumerate(norm_header) if k in _NAME_KEYS), None)
        time_idx = next((i for i, k in enumerate(norm_header) if k in _TIME_KEYS), None)
        group_idx = next((i for i, k in enumerate(norm_header) if k == _norm_key("group_id")), None)
        role_keys = {_norm_key(r) for r in ROLE_FIELDS}
        role_idx = next((i for i, k in enumerate(norm_header) if k in role_keys), None)
        if time_idx is None:
            time_idx = next(
                (i for i, k in enumerate(norm_header) if "time" in k or "date" in k), None
            )
        body = rows[1:]
    else:
        # 无表头：按 时间,QQ,昵称,内容 猜测（QQ 列取最像 QQ 号的一列）
        body = rows
        width = max(len(r) for r in rows)
        time_idx, uid_idx, name_idx, group_idx, role_idx = None, None, None, None, None
        for i in range(width):
            col = [r[i] for r in rows if len(r) > i][:50]
            if time_idx is None and all(parse_timestamp(v) is not None for v in col if v.strip()):
                time_idx = i
                continue
            if uid_idx is None and all(re.fullmatch(r"\d{5,12}", v.strip()) for v in col if v.strip()):
                uid_idx = i
                continue
            if name_idx is None and any(v.strip() and not v.strip().isdigit() for v in col):
                name_idx = i
        if uid_idx is None or time_idx is None:
            raise ConfigError(
                f"{path.name} 没有可识别的表头，无法确定 QQ 号/时间列；"
                "请使用带表头的 CSV（列名包含 user_id、time）"
            )

    for row in body:
        if not row or all(not c.strip() for c in row):
            continue

        def cell(idx: int | None) -> str:
            if idx is None or idx >= len(row):
                return ""
            return row[idx].strip()

        uid = to_user_id(cell(uid_idx))
        if not uid:
            continue
        ts = parse_timestamp(cell(time_idx))
        if ts is None:
            continue
        messages.append(
            Message(
                user_id=uid,
                nickname=cell(name_idx) or uid,
                timestamp=ts,
                group_id=to_user_id(cell(group_idx)),
                role=cell(role_idx).lower(),
                raw={},
            )
        )
    return messages


# ---------------------------------------------------------------- QQ 导出文本

_QCE_LINE = re.compile(
    r"^\s*(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})?\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+"
    r"(?P<name>.+?)\s*[（(\[](?P<uid>\d{5,12})[)）\]]\s*[:：]?\s*(?P<body>.*)$"
)
_QCE_LOOSE = re.compile(
    r"^\s*(?P<date>\d{4}[-/]\d{1,2}[-/]\d{1,2})?\s*"
    r"(?P<time>\d{1,2}:\d{2}(?::\d{2})?)\s+"
    r"(?P<name>[^:：]{1,40}?)\s*[:：]\s*(?P<body>.*)$"
)
_DATE_HEADER = re.compile(
    r"^\s*(?:={2,}\s*)?(?P<date>\d{4}[-/年]\d{1,2}[-/月]\d{1,2}日?)(?:\s*={2,})?\s*$"
)


def load_text(path: Path) -> list[Message]:
    """解析常见的 QQ 聊天记录导出文本。

    形如：
        2024-05-20
        12:30:45 张三(12345678): 晚上好
    """
    messages: list[Message] = []
    current_date: str = ""
    unparsed = 0
    for raw_line in path.read_text(encoding="utf-8-sig", errors="replace").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        header = _DATE_HEADER.match(line)
        if header:
            current_date = header.group("date").replace("年", "-").replace("月", "-").replace("日", "")
            continue
        if line.startswith(_SYSTEM_MARKERS):
            continue
        match = _QCE_LINE.match(line) or _QCE_LOOSE.match(line)
        if not match:
            unparsed += 1
            continue
        date_part = match.group("date") or current_date
        time_part = match.group("time")
        full = f"{date_part} {time_part}".strip()
        ts = parse_timestamp(full)
        if ts is None:
            unparsed += 1
            continue
        groups = match.groupdict()
        uid = to_user_id(groups.get("uid"))
        if not uid:
            uid = "name:" + re.sub(r"\s+", "", groups.get("name") or "")
        messages.append(
            Message(
                user_id=uid,
                nickname=(groups.get("name") or uid).strip(),
                timestamp=ts,
                message_id="",
                raw={"text": groups.get("body") or ""},
            )
        )
    if not messages and unparsed:
        raise ConfigError(
            f"{path.name} 无法解析出任何聊天记录，请确认格式为 "
            "'12:30:45 昵称(QQ号): 内容' 且带日期行"
        )
    return messages


# ---------------------------------------------------------------- 统一入口


def _convert_txt_encoding(path: Path) -> tuple[Path, bool]:
    """腾讯导出常见 GBK 编码，必要时转存为 UTF-8 临时文件。"""
    data = path.read_bytes()
    for enc in ("utf-8-sig", "utf-8"):
        try:
            data.decode(enc)
            return path, False
        except UnicodeDecodeError:
            continue
    for enc in ("gb18030", "gbk"):
        try:
            text = data.decode(enc)
        except UnicodeDecodeError:
            continue
        tmp = path.with_suffix(path.suffix + ".utf8.tmp")
        tmp.write_text(text, encoding="utf-8")
        return tmp, True
    return path, False


def load_from_file(path: str | Path, fmt: str = "auto", group_id: str = "") -> list[Message]:
    file_path = Path(path)
    if not file_path.is_file():
        raise ConfigError(f"聊天记录文件不存在：{file_path}")

    actual, converted = (file_path, False)
    if file_path.suffix.lower() in (".txt", ".log", ""):
        actual, converted = _convert_txt_encoding(file_path)

    suffix = actual.suffix.lower()
    resolved = fmt
    if fmt in ("auto", ""):
        if suffix in (".json", ".jsonl", ".ndjson"):
            resolved = "json"
        elif suffix in (".csv", ".tsv"):
            resolved = "csv"
        else:
            resolved = "text"

    try:
        if resolved == "json":
            messages = load_json(actual)
        elif resolved == "csv":
            messages = load_delimited(actual, "\t" if suffix == ".tsv" else None)
        elif resolved == "text":
            messages = load_text(actual)
        else:
            raise ConfigError(f"不支持的文件格式：{fmt!r}（可选 auto/json/csv/text）")
    finally:
        if converted:
            actual.unlink(missing_ok=True)

    if group_id:
        filtered = [m for m in messages if not m.group_id or m.group_id == group_id]
        if filtered:
            messages = filtered

    messages.sort(key=lambda m: m.timestamp)
    return messages


# ---------------------------------------------------------------- OneBot v11

try:  # Python 3.8+ 标准库
    from urllib import error as urlerror
    from urllib import request as urlrequest
except ImportError:  # pragma: no cover
    urlrequest = None  # type: ignore
    urlerror = None  # type: ignore


class OneBotError(RuntimeError):
    """调用 OneBot 接口失败。"""


class OneBotClient:
    """OneBot v11 HTTP 客户端（NapCat / Lagrange / go-cqhttp / LLOneBot 通用）。"""

    def __init__(self, base_url: str, access_token: str = "", timeout: float = 15.0) -> None:
        self.base_url = base_url.rstrip("/")
        self.access_token = access_token
        self.timeout = timeout

    def call(self, action: str, params: dict[str, Any] | None = None) -> Any:
        if urlrequest is None:  # pragma: no cover
            raise OneBotError("当前 Python 缺少 urllib，无法访问 OneBot 接口")
        url = f"{self.base_url}/{action.lstrip('/')}"
        payload = json.dumps(params or {}, ensure_ascii=False).encode("utf-8")
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if self.access_token:
            headers["Authorization"] = f"Bearer {self.access_token}"
        req = urlrequest.Request(url, data=payload, headers=headers, method="POST")
        try:
            with urlrequest.urlopen(req, timeout=self.timeout) as resp:
                body = resp.read().decode("utf-8", errors="replace")
        except urlerror.HTTPError as exc:  # type: ignore[union-attr]
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            raise OneBotError(
                f"调用 {action} 失败：HTTP {exc.code}。"
                f"请检查 base_url 与 access_token 是否正确。响应：{detail}"
            ) from exc
        except urlerror.URLError as exc:  # type: ignore[union-attr]
            raise OneBotError(
                f"无法连接 OneBot 服务 {self.base_url}（{exc.reason}）。"
                "请确认 NapCat/Lagrange 已启动并开启 HTTP 服务端。"
            ) from exc
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise OneBotError(f"{action} 返回的不是 JSON：{body[:200]}") from exc
        if isinstance(data, dict) and data.get("status") in ("failed", "error"):
            raise OneBotError(f"{action} 返回失败：{data.get('message') or data.get('wording') or data}")
        if isinstance(data, dict) and "retcode" in data and int(data.get("retcode") or 0) not in (0, 1):
            raise OneBotError(
                f"{action} 返回错误码 {data.get('retcode')}：{data.get('message') or data.get('wording') or ''}"
            )
        if isinstance(data, dict) and "data" in data:
            return data["data"]
        return data

    # ---- 具体接口 ----
    def get_group_msg_history(
        self, group_id: str, count: int = 200, message_seq: Any = None
    ) -> list[dict[str, Any]]:
        params: dict[str, Any] = {"group_id": _as_number(group_id), "count": int(count)}
        if message_seq is not None:
            params["message_seq"] = _as_number(message_seq)
        data = self.call("get_group_msg_history", params)
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        if isinstance(data, dict):
            for key in ("messages", "message", "list", "data"):
                inner = data.get(key)
                if isinstance(inner, list):
                    return [m for m in inner if isinstance(m, dict)]
        return []

    def get_group_member_list(self, group_id: str) -> list[dict[str, Any]]:
        data = self.call("get_group_member_list", {"group_id": _as_number(group_id)})
        if isinstance(data, list):
            return [m for m in data if isinstance(m, dict)]
        return []

    def send_group_msg(self, group_id: str, message: str) -> Any:
        # 硬锁优先于一切配置：测试环境绝不能把消息发进真实的群
        guard_send("send_group_msg", f"group {group_id}")
        return self.call("send_group_msg", {"group_id": _as_number(group_id), "message": message})


def _as_number(value: Any) -> Any:
    text = to_user_id(value)
    return int(text) if text.isdigit() else value


def load_from_onebot(
    cfg: Config,
    since: float | None = None,
    progress: Any = None,
) -> list[Message]:
    """向前翻页拉取群消息，直到超出时间窗口或达到上限。"""
    client = OneBotClient(cfg.onebot_base_url, cfg.onebot_access_token)
    group_id = cfg.group_id
    messages: list[Message] = []
    seen: set[str] = set()
    sentinel: Any = None
    stop = False

    for page in range(1, cfg.onebot_max_pages + 1):
        if stop:
            break
        batch = client.get_group_msg_history(group_id, cfg.onebot_page_size, sentinel)
        if not batch:
            break

        parsed: list[Message] = []
        for row in batch:
            msg = row_to_message(row)
            if not msg:
                continue
            key = msg.message_id or f"{msg.user_id}:{msg.timestamp:.3f}"
            if key in seen:
                continue
            seen.add(key)
            parsed.append(msg)
        if not parsed:
            break

        messages.extend(parsed)
        oldest = min(parsed, key=lambda m: m.timestamp)
        if progress:
            progress(
                f"已拉取第 {page} 页，累计 {len(messages)} 条，最早 {oldest.dt:%Y-%m-%d %H:%M}"
            )

        if since is not None and oldest.timestamp < since:
            stop = True  # 已翻到窗口之外
        if len(messages) >= cfg.onebot_fetch_limit:
            stop = True

        # 下一页游标：优先用 message_seq，其次 message_id
        next_seq = None
        for row in batch:
            seq = row.get("message_seq")
            if seq is not None:
                seq_num = _as_number(seq)
                if next_seq is None or (isinstance(seq_num, int) and seq_num < next_seq):
                    next_seq = seq_num
        if next_seq is None:
            oldest_raw = min(batch, key=lambda r: parse_timestamp(_pick(r, TIME_FIELDS)) or 0)
            next_seq = oldest_raw.get("message_id")
        if next_seq is None or next_seq == sentinel:
            break
        sentinel = next_seq

    if cfg.onebot_exclude_self:
        try:
            login = client.call("get_login_info") or {}
            self_id = to_user_id(login.get("user_id") if isinstance(login, dict) else None)
        except OneBotError:
            self_id = ""
        if self_id:
            messages = [m for m in messages if m.user_id != self_id]

    messages.sort(key=lambda m: m.timestamp)
    return messages


# ---------------------------------------------------------------- 过滤辅助


def looks_like_bot(user_id: str, nickname: str) -> bool:
    """粗略判断机器人/系统账号。"""
    if user_id in BOT_UIDS:
        return True
    low = (nickname or "").lower()
    return any(hint in low for hint in BOT_NAME_HINTS)


def within_window(messages: Iterable[Message], start: float, end: float | None = None) -> list[Message]:
    return [m for m in messages if m.timestamp >= start and (end is None or m.timestamp <= end)]
