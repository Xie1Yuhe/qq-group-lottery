"""抽奖核心：活跃度统计、候选筛选、加权随机抽取。"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Iterable, Sequence

from .config import Config
from .model import Activity, Message, Winner, now_ts
from .sources import looks_like_bot

# 数据比当前时间旧超过这个秒数时，认为记录是离线导出而非实时数据，
# 于是把统计窗口锚定到最近一条消息，避免算出一份空名单。
STALE_TOLERANCE_SECONDS = 1800.0


@dataclass
class DrawContext:
    """一次抽奖的统计上下文。"""

    window_start: float
    window_end: float
    reference: str  # 时间窗口的参照点说明
    total_messages: int
    in_window_messages: int
    activities: list[Activity]
    candidates: list[Activity]
    excluded: dict[str, list[str]]  # 原因 -> 名字列表
    notes: list[str]
    coverage: dict[str, Any] = field(default_factory=dict)  # 数据覆盖情况（公平性前提）

    @property
    def window_desc(self) -> str:
        start = datetime.fromtimestamp(self.window_start)
        end = datetime.fromtimestamp(self.window_end)
        return f"{start:%Y-%m-%d %H:%M} ~ {end:%Y-%m-%d %H:%M}"


def window_coverage(messages: Sequence[Message], start: float, end: float) -> dict[str, Any]:
    """检查"手里这批消息"到底覆盖了统计窗口的多少。

    这是公平性的关键前提：如果消息只覆盖了窗口的一小部分（典型情况是
    NapCat 刚上线、本地缓存还很浅），那么"近三天发言"实际上并不成立，
    必须明确告诉用户，而不是悄悄按不完整的数据开奖。

    两头都要查：
      · 开头空档 —— 机器人上线太晚，窗口开头的发言没收到
      · 末尾空档 —— 机器人中途掉线，最近的发言没收到
    """
    span = max(end - start, 1.0)
    tolerance = 300.0  # 5 分钟内的滞后期望值，不算缺口

    if not messages:
        return {
            "complete": False,
            "coverage": 0.0,
            "earliest": None,
            "latest": None,
            "missing_seconds": span,
            "trailing_seconds": 0.0,
            "message": "完全没有读取到消息，无法开奖",
        }

    earliest = min(m.timestamp for m in messages)
    latest = max(m.timestamp for m in messages)

    head_gap = max(min(earliest, end) - start, 0.0)          # 开头缺多久
    tail_gap = max(end - max(latest, start), 0.0)            # 末尾缺多久
    # 容差以内的滞后不算缺口（消息本来就是陆续到达的）
    real_head = max(head_gap - tolerance, 0.0)
    real_tail = max(tail_gap - tolerance, 0.0)
    covered = max(span - real_head - real_tail, 0.0)
    ratio = min(covered / span, 1.0)
    complete = head_gap <= tolerance and tail_gap <= tolerance

    if complete:
        message = ""
    elif tail_gap > tolerance:
        message = (
            f"最近的发言没有被采集到：最后一条消息是 "
            f"{datetime.fromtimestamp(latest):%Y-%m-%d %H:%M}，距窗口终点还差 "
            f"{tail_gap / 3600:.1f} 小时。说明机器人中途停止工作了，"
            "这段时间的发言缺失。"
        )
    else:
        message = (
            f"消息只覆盖了统计窗口的 {ratio * 100:.1f}%：最早一条是 "
            f"{datetime.fromtimestamp(earliest):%Y-%m-%d %H:%M}，比窗口起点晚 "
            f"{head_gap / 3600:.1f} 小时。这说明机器人（NapCat）上线时间还不够久，"
            "窗口开头那段时间的发言没有被采集到。"
        )
    return {
        "complete": complete,
        "coverage": round(ratio, 6),
        "earliest": earliest,
        "latest": latest,
        "missing_seconds": round(head_gap, 1),
        "trailing_seconds": round(tail_gap, 1),
        "message": message,
    }


def _display(act: Activity) -> str:
    return f"{act.nickname}({act.user_id})"


def resolve_window(cfg: Config, messages: Sequence[Message], now: float | None = None) -> tuple[float, float, str]:
    """确定统计窗口 [start, end]。

    以往常做法：窗口结束点取「当前时间」。但当聊天记录是离线导出、
    且数据明显早于当前时间时，自动改为以最近一条消息的时间为终点，
    否则会算出空名单。
    """
    reference_now = now_ts() if now is None else now
    latest = max((m.timestamp for m in messages), default=None)
    end = reference_now
    reason = "以当前时间为窗口终点"
    if latest is not None and reference_now - latest > STALE_TOLERANCE_SECONDS:
        end = latest
        reason = "记录最新消息早于当前时间，自动以最近一条消息时间为窗口终点"

    # auto 模式：把窗口起点对齐到"缓存里最早的一条消息"，
    # 这样"窗口内数据完整"这一公平性前提必然成立。
    if cfg.window_mode == "auto":
        earliest = min((m.timestamp for m in messages), default=None)
        if earliest is not None and earliest > end - cfg.window_seconds:
            return earliest, end, (
                f"自动适配模式：以缓存里最早一条消息（{datetime.fromtimestamp(earliest):%Y-%m-%d %H:%M}）"
                "作为窗口起点，保证窗口内数据完整"
            )
        reason = "自动适配模式：缓存已覆盖所请求的时段，按请求窗口计算"

    return end - cfg.window_seconds, end, reason


def aggregate(
    messages: Iterable[Message],
    start: float,
    end: float,
) -> tuple[list[Activity], int]:
    """统计窗口内每人发言条数。返回 (活跃度列表, 窗口内消息数)。"""
    buckets: dict[str, Activity] = {}
    in_window = 0
    for msg in messages:
        if msg.timestamp < start or msg.timestamp > end:
            continue
        in_window += 1
        act = buckets.get(msg.user_id)
        if act is None:
            act = Activity(user_id=msg.user_id, nickname=msg.nickname, first_ts=msg.timestamp, last_ts=msg.timestamp)
            buckets[msg.user_id] = act
        act.count += 1
        if msg.timestamp < act.first_ts:
            act.first_ts = msg.timestamp
        if msg.timestamp >= act.last_ts:
            act.last_ts = msg.timestamp
            act.nickname = msg.nickname or act.nickname
            if msg.role:
                act.role = msg.role
    activities = sorted(buckets.values(), key=lambda a: (-a.count, a.user_id))
    return activities, in_window


def filter_candidates(
    cfg: Config,
    activities: Sequence[Activity],
    strict_blacklist: bool = True,
) -> tuple[list[Activity], dict[str, list[str]], list[str]]:
    """应用黑名单 / 白名单 / 最低发言数 / 机器人过滤 / 身份过滤。"""
    blacklist = set(cfg.blacklist)
    whitelist = set(cfg.whitelist)
    excluded: dict[str, list[str]] = {}
    notes: list[str] = []
    candidates: list[Activity] = []

    if whitelist:
        notes.append(
            f"已启用白名单（{len(whitelist)} 人），候选范围仅限白名单内且近 {cfg.days:g} 天有发言的群友"
        )

    for act in activities:
        reasons: list[str] = []
        if whitelist and act.user_id not in whitelist:
            reasons.append("不在白名单")
        if act.user_id in blacklist:
            reasons.append("黑名单")
        if act.count < cfg.min_messages:
            reasons.append(f"发言少于 {cfg.min_messages} 条")
        if act.role and act.role in cfg.exclude_roles:
            reasons.append(f"身份为 {act.role}")
        if looks_like_bot(act.user_id, act.nickname):
            reasons.append("疑似机器人账号")

        if reasons:
            for reason in reasons:
                excluded.setdefault(reason, []).append(_display(act))
        else:
            candidates.append(act)

    # 黑名单里但本次根本没发言的人，单独提示，避免"我明明拉黑了却没生效"的误会
    if strict_blacklist and blacklist:
        active_ids = {a.user_id for a in activities}
        silent = [uid for uid in blacklist if uid not in active_ids]
        if silent:
            notes.append(f"黑名单中 {len(silent)} 人近 {cfg.days:g} 天本来就没发言：" + "、".join(silent))

    return candidates, excluded, notes


def compute_weights(cfg: Config, candidates: Sequence[Activity]) -> None:
    """计算每人的权重（就地写入 weight_input）。"""
    for act in candidates:
        if cfg.weighting:
            weight = float(act.count)
            if cfg.weight_cap > 0:
                weight = min(weight, float(cfg.weight_cap))
            act.weight_input = max(weight, 0.0)
        else:
            act.weight_input = 1.0


def draw_winners(
    candidates: Sequence[Activity],
    winners: int,
    cfg: Config,
    rng: random.Random,
) -> list[Winner]:
    """无放回加权随机抽取（一人最多中一次）。"""
    pool = [a for a in candidates if a.weight_input > 0]
    results: list[Winner] = []
    count = min(winners, len(pool))

    for rank in range(1, count + 1):
        total = sum(a.weight_input for a in pool)
        if total <= 0:
            break
        pick = rng.random() * total
        cursor = 0.0
        chosen = pool[-1]
        for act in pool:
            cursor += act.weight_input
            if pick <= cursor:
                chosen = act
                break
        probability = chosen.weight_input / total
        results.append(
            Winner(
                rank=rank,
                user_id=chosen.user_id,
                nickname=chosen.nickname,
                count=chosen.count,
                probability=probability,
                last_speak=f"{chosen.last_dt:%Y-%m-%d %H:%M}",
            )
        )
        pool = [a for a in pool if a is not chosen]
    return results


def build_context(
    cfg: Config,
    messages: Sequence[Message],
    now: float | None = None,
) -> DrawContext:
    """完整走一遍统计 + 筛选，供 draw / stats 共用。"""
    start, end, reference = resolve_window(cfg, messages, now=now)
    activities, in_window = aggregate(messages, start, end)
    candidates, excluded, notes = filter_candidates(cfg, activities)
    compute_weights(cfg, candidates)

    # 公平性前提：手里的数据是否真的覆盖了整个统计窗口
    coverage = window_coverage(messages, start, end)
    if coverage.get("message"):
        notes.append(coverage["message"])

    if not messages:
        notes.append("没有读取到任何消息记录")
    elif in_window == 0:
        notes.append(
            f"近 {cfg.days:g} 天（{datetime.fromtimestamp(start):%Y-%m-%d %H:%M} 起）没有任何消息，"
            "请检查数据源是否覆盖该时间段"
        )
    if candidates and max(a.weight_input for a in candidates) <= 0:
        notes.append("候选人的权重全为 0，请检查 weight_cap / min_messages 配置")
    if cfg.weighting and cfg.weight_cap and candidates:
        capped = [a for a in candidates if a.count > cfg.weight_cap]
        if capped:
            notes.append(
                f"有 {len(capped)} 人发言条数超过权重上限 {cfg.weight_cap}，其权重已按上限封顶"
            )

    return DrawContext(
        window_start=start,
        window_end=end,
        reference=reference,
        total_messages=len(messages),
        in_window_messages=in_window,
        activities=activities,
        candidates=candidates,
        excluded=excluded,
        notes=notes,
        coverage=coverage,
    )


def run_draw(
    cfg: Config, messages: Sequence[Message], now: float | None = None
) -> tuple[list[Winner], DrawContext, int]:
    """执行一次抽奖。

    返回 (中奖名单, 统计上下文, 本次使用的随机种子)。
    """
    ctx = build_context(cfg, messages, now=now)
    seed = cfg.seed
    if seed is None:
        seed = random.SystemRandom().getrandbits(63)
    rng = random.Random(seed)
    winners = draw_winners(ctx.candidates, cfg.winners, cfg, rng)
    return winners, ctx, int(seed)


def fairness_report(
    cfg: Config,
    ctx: DrawContext,
    winners: Sequence[Winner],
    seed: int,
    coverage: dict[str, Any],
) -> dict[str, Any]:
    """把"这次抽奖凭什么公平"写成一份可核对、可复现的记录。

    包含：候选范围、每个人的权重与概率、随机方式与种子、是否去重、
    以及数据覆盖是否完整。存进历史记录后，任何人都能拿着同一批数据
    和同一个种子复算出同样的名单。
    """
    total_weight = sum(a.weight_input for a in ctx.candidates)
    return {
        "method": "weighted_random_sampling_without_replacement"
        if cfg.weighting
        else "uniform_random_sampling_without_replacement",
        "method_label": "按发言条数加权，无放回抽取（一人最多中一次）"
        if cfg.weighting
        else "每人等概率，无放回抽取（一人最多中一次）",
        "algorithm": "each-round draws r ~ U(0, total_weight) and walks cumulative weights",
        "reproducible": True,
        "seed": seed,
        "seed_source": "配置固定种子" if cfg.seed is not None else "系统加密随机源（每次不同）",
        "candidate_count": len(ctx.candidates),
        "total_weight": round(total_weight, 6),
        "weight_cap": cfg.weight_cap or None,
        "min_messages": cfg.min_messages,
        "dedup": True,
        "data_coverage": {
            "complete": coverage.get("complete", False),
            "coverage": coverage.get("coverage", 0.0),
            "window_start": datetime.fromtimestamp(ctx.window_start).strftime("%Y-%m-%d %H:%M:%S"),
            "window_end": datetime.fromtimestamp(ctx.window_end).strftime("%Y-%m-%d %H:%M:%S"),
            "earliest_message": (
                datetime.fromtimestamp(coverage["earliest"]).strftime("%Y-%m-%d %H:%M:%S")
                if coverage.get("earliest")
                else None
            ),
            "warning": coverage.get("message", ""),
        },
        "weights": [
            {
                "user_id": w.user_id,
                "nickname": w.nickname,
                "weight": round(
                    next((a.weight_input for a in ctx.candidates if a.user_id == w.user_id), 0.0), 6
                ),
                "pick_probability": round(w.probability, 8),
            }
            for w in winners
        ],
    }
