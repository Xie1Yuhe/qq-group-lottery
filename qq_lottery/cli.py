"""命令行入口。

用法示例：
    python -m qq_lottery draw                # 按 config.json 抽奖
    python -m qq_lottery draw -n 3 --days 3  # 抽 3 人
    python -m qq_lottery stats               # 只看活跃度预览，不抽奖
    python -m qq_lottery fetch               # 从机器人拉消息存成文件
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Any

from . import __version__
from .config import Config, ConfigError, parse_time_window
from .lottery import build_context, run_draw
from .paths import resolve as resolve_path
from .report import (
    append_history,
    render_group_message,
    render_json,
    render_text,
    write_csv,
)
from .sources import OneBotError, SendBlockedError, load_from_file, load_from_onebot


def _setup_console() -> None:
    """让 Windows 控制台正确输出中文与 emoji。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if os.name == "nt":
        try:
            import ctypes

            ctypes.windll.kernel32.SetConsoleOutputCP(65001)
        except Exception:  # pragma: no cover
            pass


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="qq_lottery",
        description="QQ 群活跃群友随机抽奖工具（默认范围：近三天内发过言的群友）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python -m qq_lottery draw --source onebot --group 123456789 -n 3\n"
            "  python -m qq_lottery draw --source file --file data/messages.json\n"
            "  python -m qq_lottery draw --days 72h --seed 20240520 --csv out/result.csv\n"
        ),
    )
    parser.add_argument("-v", "--version", action="version", version=f"qq_lottery {__version__}")
    sub = parser.add_subparsers(dest="command")

    def add_common(p: argparse.ArgumentParser) -> None:
        p.add_argument("-c", "--config", help="配置文件路径（默认读取工作目录下的 config.json）")
        p.add_argument("-s", "--source", choices=["file", "onebot"], help="数据来源")
        p.add_argument("-g", "--group", dest="group_id", help="QQ 群号")
        p.add_argument("-d", "--days", help="统计窗口，如 3 / 3d / 72h")
        p.add_argument("--file", dest="file_path", help="离线聊天记录文件路径")
        p.add_argument("--format", dest="file_format", choices=["auto", "json", "csv", "text"], help="文件格式")
        p.add_argument("--no-weight", dest="weighting", action="store_false", default=None,
                       help="关闭按发言条数加权，改为每人等概率")
        p.add_argument("--weight-cap", type=int, help="单人权重上限（条数），0 表示不限制")
        p.add_argument("--min-messages", type=int, help="最低发言条数门槛")
        p.add_argument("--blacklist", help="追加黑名单，逗号分隔的 QQ 号")
        p.add_argument("--exclude-role", help="追加排除身份，逗号分隔（owner,admin）")
        p.add_argument("--onebot-url", help="OneBot HTTP 地址，如 http://127.0.0.1:3000")
        p.add_argument("--access-token", help="OneBot access_token")
        p.add_argument("-q", "--quiet", action="store_true", help="只输出中奖名单")

    draw = sub.add_parser("draw", help="执行抽奖", formatter_class=argparse.RawDescriptionHelpFormatter)
    add_common(draw)
    draw.add_argument("-n", "--winners", type=int, help="抽取人数")
    draw.add_argument("--seed", type=int, help="随机种子（可复现结果）")
    draw.add_argument("--json", dest="json_out", nargs="?", const="-", help="输出 JSON 结果（可跟文件名）")
    draw.add_argument("--csv", dest="csv_out", help="把中奖名单写入 CSV 文件")
    draw.add_argument("--history", dest="history_out", nargs="?", const="", help="把本次结果追加到历史记录文件")
    draw.add_argument("--no-history", action="store_true", help="不写入历史记录")
    draw.add_argument("--top", type=int, default=10, help="控制台展示活跃度排行前几名（默认 10）")
    draw.add_argument("--post", action="store_true", help="把开奖结果发送到群里（需 onebot 数据源）")

    stats = sub.add_parser("stats", help="只统计活跃度，不抽奖")
    add_common(stats)
    stats.add_argument("--top", type=int, default=20, help="展示前几名（默认 20）")

    fetch = sub.add_parser("fetch", help="从机器人拉取消息并保存为文件")
    add_common(fetch)
    fetch.add_argument("-o", "--out", default="data/messages.json", help="输出文件路径")
    fetch.add_argument("--jsonl", action="store_true", help="以 JSONL（每行一条）保存，适合大数据量")

    watch = sub.add_parser(
        "watch",
        help="无界面常驻：持续把群消息增量存入本地存档（专门负责攒历史）",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "示例：\n"
            "  python run_lottery.py watch                 # 按配置间隔常驻同步\n"
            "  python run_lottery.py watch --interval 120  # 改成每 2 分钟\n"
            "  python run_lottery.py watch --once          # 只同步一次就退出\n"
            "  python run_lottery.py watch --rounds 3      # 只跑 3 轮（自检/试跑用）\n"
        ),
    )
    add_common(watch)
    watch.add_argument("--interval", type=int, help="同步间隔（秒），覆盖配置里的值")
    watch.add_argument("--once", action="store_true", help="只同步一次就退出")
    watch.add_argument("--rounds", type=int, help="最多跑几轮后退出")
    watch.add_argument("--log", help="日志文件路径（默认 data/archive-watch.log）")
    watch.add_argument("--silent", action="store_true", help="不打印到控制台，只写日志")

    return parser


def _overrides(args: argparse.Namespace) -> dict[str, Any]:
    data: dict[str, Any] = {}
    if getattr(args, "source", None):
        data["source"] = args.source
    if getattr(args, "group_id", None):
        data["group_id"] = args.group_id
    if getattr(args, "days", None):
        data["days"] = parse_time_window(args.days) / 86400.0
    if getattr(args, "weighting", None) is not None:
        data["weighting"] = args.weighting
    if getattr(args, "weight_cap", None) is not None:
        data["weight_cap"] = args.weight_cap
    if getattr(args, "min_messages", None) is not None:
        data["min_messages"] = args.min_messages
    if getattr(args, "seed", None) is not None:
        data["seed"] = args.seed
    if getattr(args, "onebot_url", None):
        data.setdefault("onebot", {})["base_url"] = args.onebot_url
    if getattr(args, "access_token", None):
        data.setdefault("onebot", {})["access_token"] = args.access_token
    if getattr(args, "file_path", None) or getattr(args, "file_format", None):
        file_cfg: dict[str, Any] = data.setdefault("file", {})
        if args.file_path:
            file_cfg["path"] = args.file_path
        if args.file_format:
            file_cfg["format"] = args.file_format
    return data


def _split_ids(text: str | None) -> list[str]:
    if not text:
        return []
    return [part.strip() for part in text.replace("，", ",").split(",") if part.strip()]


def _log(message: str, quiet: bool = False) -> None:
    if not quiet:
        print(f"[qq_lottery] {message}", file=sys.stderr)


def _load_messages(cfg: Config, quiet: bool = False) -> list[Any]:
    if cfg.source == "onebot":
        # 群号没填这类问题，先给一句人话，而不是让机器人接口报莫名其妙的错
        from .service import ready_to_pull

        problem = ready_to_pull(cfg)
        if problem:
            raise ConfigError(problem)
        _log(f"正在从 {cfg.onebot_base_url} 拉取群 {cfg.group_id} 的消息…", quiet)
        messages = load_from_onebot(cfg, progress=lambda text: _log(text, quiet))
        _log(f"拉取完成，共 {len(messages)} 条消息", quiet)
        return messages
    _log(f"正在解析聊天记录文件 {cfg.file_path_abs} …", quiet)
    messages = load_from_file(cfg.file_path_abs, cfg.file_format, cfg.group_id)
    _log(f"解析完成，共 {len(messages)} 条消息", quiet)
    return messages


def _apply_cli_lists(cfg: Config, args: argparse.Namespace) -> None:
    extra_black = _split_ids(getattr(args, "blacklist", None))
    if extra_black:
        cfg.blacklist = list(dict.fromkeys(cfg.blacklist + extra_black))
    extra_roles = _split_ids(getattr(args, "exclude_role", None))
    if extra_roles:
        cfg.exclude_roles = list(dict.fromkeys(cfg.exclude_roles + [r.lower() for r in extra_roles]))


def cmd_draw(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config, _overrides(args))
    _apply_cli_lists(cfg, args)
    if args.winners is not None:
        if args.winners < 1:
            raise ConfigError("抽取人数必须 >= 1")
        cfg.winners = args.winners
    if args.post:
        cfg.post_to_group = True
        if cfg.source != "onebot":
            raise ConfigError("--post 需要配合 --source onebot 使用")

    messages = _load_messages(cfg, args.quiet)
    winners, ctx, seed = run_draw(cfg, messages)

    if args.json_out is not None:
        payload = render_json(cfg, ctx, winners, seed)
        text = json.dumps(payload, ensure_ascii=False, indent=2)
        if args.json_out and args.json_out != "-":
            out = Path(args.json_out)
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(text, encoding="utf-8")
            _log(f"JSON 结果已写入 {out}", args.quiet)
        else:
            print(text)
    if args.csv_out and winners:
        path = write_csv(args.csv_out, cfg, ctx, winners, seed)
        _log(f"CSV 名单已写入 {path}", args.quiet)

    if args.quiet:
        for win in winners:
            print(f"{win.rank}\t{win.user_id}\t{win.nickname}\t{win.count}\t{win.probability * 100:.2f}%")
        if not winners:
            _log("没有符合条件的候选群友，未抽出中奖者", True)
            print("（无中奖者）")
    else:
        print(render_text(cfg, ctx, winners, seed, top=args.top))
        if not winners:
            print("\n提示：可用 `python -m qq_lottery stats` 查看实际读到的活跃排行与排除原因。")

    if cfg.post_to_group and winners:
        from .sources import OneBotClient

        client = OneBotClient(cfg.onebot_base_url, cfg.onebot_access_token)
        client.send_group_msg(cfg.group_id, render_group_message(cfg, ctx, winners))
        _log("开奖结果已发送到群", args.quiet)

    if not args.no_history:
        target = resolve_path(args.history_out) if args.history_out else cfg.history_file_abs
        if target:
            append_history(target, render_json(cfg, ctx, winners, seed))
            _log(f"本次结果已追加到历史记录 {target}", args.quiet)

    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = Config.load(args.config, _overrides(args))
    _apply_cli_lists(cfg, args)
    messages = _load_messages(cfg, args.quiet)
    ctx = build_context(cfg, messages)

    print("=" * 58)
    print("  QQ 群活跃度统计")
    print("=" * 58)
    if cfg.group_id:
        print(f"  群号        : {cfg.group_id}")
    print(f"  统计窗口    : 近 {cfg.days:g} 天（{ctx.window_desc}）")
    print(f"  窗口参照    : {ctx.reference}")
    print(f"  已读消息    : {ctx.total_messages} 条，其中窗口内 {ctx.in_window_messages} 条")
    print(f"  发过言的人  : {len(ctx.activities)} 人")
    print(f"  符合条件    : {len(ctx.candidates)} 人")
    print("-" * 58)
    if ctx.candidates:
        print(f"  {'名次':<4}{'昵称':<18}{'QQ号':<14}{'发言':<6}最后发言")
        for idx, act in enumerate(ctx.candidates[: args.top], start=1):
            nick = act.nickname if len(act.nickname) <= 16 else act.nickname[:15] + "…"
            pad = 18 - sum(2 if ord(ch) > 127 else 1 for ch in nick)
            print(
                f"  {idx:<4}{nick}{' ' * max(pad, 1)}{act.user_id:<14}{act.count:<6}{act.last_dt:%Y-%m-%d %H:%M}"
            )
        if len(ctx.candidates) > args.top:
            print(f"  … 其余 {len(ctx.candidates) - args.top} 人省略")
    else:
        print("  没有符合条件的群友。请检查时间窗口、数据源与过滤条件。")

    if ctx.excluded:
        print("-" * 58)
        print("  已排除")
        for reason, people in ctx.excluded.items():
            print(f"  · {reason}（{len(people)} 人）：{'、'.join(people[:6])}")
    if ctx.notes:
        print("-" * 58)
        for note in ctx.notes:
            print(f"  ℹ️ {note}")
    print("=" * 58)
    return 0


def cmd_fetch(args: argparse.Namespace) -> int:
    overrides = _overrides(args)
    overrides["source"] = "onebot"
    cfg = Config.load(args.config, overrides)
    messages = _load_messages(cfg, args.quiet)

    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    if args.jsonl or out.suffix.lower() in (".jsonl", ".ndjson"):
        with out.open("w", encoding="utf-8") as fh:
            for msg in messages:
                fh.write(
                    json.dumps(
                        {
                            "group_id": msg.group_id or cfg.group_id,
                            "user_id": msg.user_id,
                            "nickname": msg.nickname,
                            "role": msg.role,
                            "time": int(msg.timestamp),
                            "message_id": msg.message_id,
                        },
                        ensure_ascii=False,
                    )
                    + "\n"
                )
    else:
        payload = [
            {
                "group_id": msg.group_id or cfg.group_id,
                "user_id": msg.user_id,
                "nickname": msg.nickname,
                "role": msg.role,
                "time": int(msg.timestamp),
                "message_id": msg.message_id,
            }
            for msg in messages
        ]
        out.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")

    span = ""
    if messages:
        span = f"，时间范围 {datetime.fromtimestamp(messages[0].timestamp):%Y-%m-%d %H:%M} ~ {datetime.fromtimestamp(messages[-1].timestamp):%Y-%m-%d %H:%M}"
    print(f"已保存 {len(messages)} 条消息到 {out}{span}")
    return 0


def cmd_watch(args: argparse.Namespace) -> int:
    """无界面常驻：持续把消息增量写入本地存档。"""
    import threading

    from .watch import WatchLogger, build_service, install_signal_handlers, run_forever, run_once

    cfg = Config.load(args.config, _overrides(args))
    service = build_service(args.config)

    log_path = resolve_path(args.log) if args.log else resolve_path("data/archive-watch.log")
    logger = WatchLogger(log_path, echo=not args.silent)

    if not cfg.archive_enabled:
        print(
            "存档当前是关闭的（config.json 里 archive.enabled = false）。\n"
            "请先在界面或配置里打开「消息存档」，再运行 watch。",
            file=sys.stderr,
        )
        return 2
    if cfg.source != "onebot":
        print("常驻同步只对机器人数据源有意义（当前是离线文件模式）。", file=sys.stderr)
        return 2

    interval = int(args.interval or cfg.archive_interval)
    logger.write(
        f"启动常驻同步：群 {cfg.group_id or '(未设置)'}，间隔 {interval} 秒，"
        f"存档 {paths_display(cfg.archive_path)}，日志 {log_path}"
    )

    if args.once:
        event = run_once(service, logger)
        service.close_archives()
        return 0 if event.ok else 1

    stop = threading.Event()
    install_signal_handlers(stop, logger)
    try:
        result = run_forever(
            service,
            logger,
            interval=interval,
            force_interval=bool(args.interval),
            max_rounds=args.rounds,
            stop_event=stop,
        )
    except KeyboardInterrupt:
        result = None
        logger.write("收到 Ctrl+C，已停止")
    finally:
        # 退出前关闭连接，确保最后一轮同步的 WAL 内容落盘
        service.close_archives()
    logger.write(f"常驻同步结束：{'已停止' if result is None else result.summary()}")
    return 0


def paths_display(value: str) -> str:
    from .paths import display as _display

    return _display(value)


def main(argv: list[str] | None = None) -> int:
    _setup_console()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return 0
    handlers = {"draw": cmd_draw, "stats": cmd_stats, "fetch": cmd_fetch, "watch": cmd_watch}
    try:
        return handlers[args.command](args)
    except ConfigError as exc:
        print(f"配置错误：{exc}", file=sys.stderr)
        return 2
    except OneBotError as exc:
        print(f"机器人接口错误：{exc}", file=sys.stderr)
        return 3
    except SendBlockedError as exc:
        # 发送被安全锁拒绝：说明当前处于"禁止发消息"的测试环境
        print(f"发送已被禁止：{exc}", file=sys.stderr)
        return 4
    except FileNotFoundError as exc:
        print(f"文件不存在：{exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n已取消", file=sys.stderr)
        return 130


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
