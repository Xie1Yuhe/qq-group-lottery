"""结果渲染：控制台文本 / JSON / CSV / 群消息文案。"""

from __future__ import annotations

import csv
import json
from datetime import datetime
from pathlib import Path
from typing import Any, Sequence

from .config import Config
from .lottery import DrawContext
from .model import Winner


def _ts(value: float) -> str:
    return datetime.fromtimestamp(value).strftime("%Y-%m-%d %H:%M:%S")


def render_text(
    cfg: Config,
    ctx: DrawContext,
    winners: Sequence[Winner],
    seed: int,
    top: int = 10,
    show_ranking: bool = True,
) -> str:
    lines: list[str] = []
    lines.append("=" * 58)
    lines.append("  QQ 群活跃群友抽奖")
    lines.append("=" * 58)
    if cfg.group_id:
        lines.append(f"  群号        : {cfg.group_id}")
    lines.append(f"  数据来源    : {'OneBot 机器人接口' if cfg.source == 'onebot' else '离线聊天记录'}")
    lines.append(f"  统计窗口    : 近 {cfg.days:g} 天（{ctx.window_desc}）")
    lines.append(f"  窗口参照    : {ctx.reference}")
    lines.append(f"  已读消息    : {ctx.total_messages} 条，其中窗口内 {ctx.in_window_messages} 条")
    lines.append(f"  候选人数    : {len(ctx.candidates)} 人")
    lines.append(f"  抽奖方式    : {'按发言条数加权' if cfg.weighting else '每人等概率'}")
    if cfg.weighting and cfg.weight_cap:
        lines.append(f"  权重上限    : {cfg.weight_cap} 条/人")
    lines.append(f"  抽取人数    : {cfg.winners} 人")
    lines.append(f"  随机种子    : {seed}（相同数据 + 相同种子结果可复现）")
    lines.append("-" * 58)

    if winners:
        lines.append("  🎉 中奖名单")
        for win in winners:
            lines.append(
                f"  [{win.rank}] {win.nickname}（{win.user_id}）"
                f"  近{cfg.days:g}天发言 {win.count} 条"
                f"  第{win.rank}轮中奖概率 {win.probability * 100:.2f}%"
            )
    else:
        lines.append("  ⚠️ 没有抽到任何人：候选范围内没有符合条件的群友")
        lines.append("     可能原因：时间窗口内无人发言 / 全部被黑名单或最低发言数过滤")

    if show_ranking and ctx.candidates:
        lines.append("-" * 58)
        lines.append(f"  活跃度排行（前 {min(top, len(ctx.candidates))} 名，仅作参考）")
        for idx, act in enumerate(ctx.candidates[:top], start=1):
            weight_note = ""
            if cfg.weighting:
                weight_note = f"  权重 {act.weight_input:g}"
            lines.append(
                f"  {idx:>2}. {act.nickname}（{act.user_id}）  {act.count} 条{weight_note}"
                f"  最后发言 {act.last_dt:%m-%d %H:%M}"
            )

    if ctx.excluded:
        lines.append("-" * 58)
        lines.append("  已排除")
        for reason, people in ctx.excluded.items():
            shown = "、".join(people[:8])
            more = f" 等 {len(people)} 人" if len(people) > 8 else ""
            lines.append(f"  · {reason}：{shown}{more}")

    # 公平性前提：数据覆盖不足必须显眼
    coverage = getattr(ctx, "coverage", {}) or {}
    if coverage and not coverage.get("complete", True):
        lines.append("-" * 58)
        lines.append(f"  ⚠️  数据覆盖不足：只有 {coverage.get('coverage', 0) * 100:.1f}% 的统计窗口有数据")
        if coverage.get("message"):
            lines.append(f"     {coverage['message']}")
        lines.append("     这不影响抽取本身的随机性，但候选范围偏窄，建议让机器人多在线一段时间再抽。")

    if ctx.notes:
        lines.append("-" * 58)
        for note in ctx.notes:
            lines.append(f"  ℹ️ {note}")

    lines.append("=" * 58)
    return "\n".join(lines)


def render_group_message(
    cfg: Config,
    ctx: DrawContext,
    winners: Sequence[Winner],
    title: str = "本群抽奖开奖啦",
) -> str:
    lines = [f"🎉 {title}"]
    lines.append(f"范围：近 {cfg.days:g} 天内有发言的群友（共 {len(ctx.candidates)} 人参与）")
    if cfg.weighting:
        lines.append("规则：发言越多中奖概率越高，每人最多中一次")
    else:
        lines.append("规则：每人等概率，每人最多中一次")
    lines.append("———————————")
    if not winners:
        lines.append("本次没有符合条件的参与者，抽奖取消。")
        return "\n".join(lines)

    icons = ["🥇", "🥈", "🥉"]
    for win in winners:
        icon = icons[win.rank - 1] if win.rank <= len(icons) else "🎁"
        lines.append(f"{icon} {win.nickname}（{win.user_id}）")
    lines.append("———————————")
    lines.append("恭喜以上群友，请私聊领取奖励～")
    return "\n".join(lines)


def render_json(
    cfg: Config,
    ctx: DrawContext,
    winners: Sequence[Winner],
    seed: int,
) -> dict[str, Any]:
    coverage = getattr(ctx, "coverage", {}) or {}
    return {
        "drawn_at": _ts(datetime.now().timestamp()),
        "group_id": cfg.group_id or None,
        "source": cfg.source,
        "days": cfg.days,
        "window": {"start": _ts(ctx.window_start), "end": _ts(ctx.window_end), "reference": ctx.reference},
        "weighting": cfg.weighting,
        "weight_cap": cfg.weight_cap or None,
        "min_messages": cfg.min_messages,
        "winners_requested": cfg.winners,
        "seed": seed,
        "messages_total": ctx.total_messages,
        "messages_in_window": ctx.in_window_messages,
        "candidate_count": len(ctx.candidates),
        # 公平性前提：数据是否覆盖了整个统计窗口
        "data_coverage": {
            "complete": bool(coverage.get("complete", False)),
            "ratio": coverage.get("coverage", 0.0),
            "earliest_message": (
                _ts(coverage["earliest"]) if coverage.get("earliest") else None
            ),
            "missing_hours": round(coverage.get("missing_seconds", 0) / 3600, 2),
            "warning": coverage.get("message", ""),
        },
        "winners": [w.as_dict() for w in winners],
        "activity_ranking": [
            {
                "user_id": a.user_id,
                "nickname": a.nickname,
                "count": a.count,
                "weight": a.weight_input,
                "last_speak": _ts(a.last_ts),
            }
            for a in ctx.candidates
        ],
        "excluded": {reason: people for reason, people in ctx.excluded.items()},
        "notes": ctx.notes,
    }


def write_csv(path: str | Path, cfg: Config, ctx: DrawContext, winners: Sequence[Winner], seed: int) -> str:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8-sig", newline="") as fh:
        writer = csv.writer(fh)
        writer.writerow(["名次", "QQ号", "昵称", "近N天发言条数", "中奖概率", "最后发言时间", "统计窗口", "随机种子"])
        for win in winners:
            writer.writerow(
                [
                    win.rank,
                    win.user_id,
                    win.nickname,
                    win.count,
                    f"{win.probability * 100:.2f}%",
                    win.last_speak,
                    ctx.window_desc,
                    seed,
                ]
            )
    return str(out)


def append_history(path: str | Path, record: dict[str, Any]) -> str:
    out = Path(path)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return str(out)
