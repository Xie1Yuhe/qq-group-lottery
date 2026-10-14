#!/usr/bin/env python3
"""端到端自检：文件解析、筛选、加权、翻页拉取、输出格式。

    python scripts/selftest.py

全部通过时退出码为 0，任意一项失败退出码为 1。
"""

from __future__ import annotations

import csv
import io
import json
import os
import random
import re
import shutil
import sys
import tempfile
import threading
import time
from contextlib import redirect_stdout
from datetime import datetime
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

# 安全锁：本进程内禁止一切向群里发消息的调用。
# 事故教训：测试曾在用户真实群配置上运行，把测试开奖文案真的发进了群。
# 有了这道锁，即便测试误用了真实配置，发送也会被拒绝而不是发出去。
os.environ["QQLOTTERY_NO_SEND"] = "1"

from qq_lottery.cli import _setup_console, main as cli_main  # noqa: E402
from qq_lottery.config import Config, ConfigError, parse_time_window, to_user_id  # noqa: E402
from qq_lottery.lottery import build_context, run_draw  # noqa: E402
from qq_lottery.model import Message  # noqa: E402
from qq_lottery.sources import load_from_file, load_from_onebot, looks_like_bot  # noqa: E402

PASSED: list[str] = []
FAILED: list[str] = []


def check(name: str, condition: bool, detail: str = "") -> None:
    if condition:
        PASSED.append(name)
        print(f"  [PASS] {name}")
    else:
        FAILED.append(name)
        print(f"  [FAIL] {name} {detail}")


def section(title: str) -> None:
    print(f"\n== {title} ==")


# 隔离用的测试配置路径。所有 CLI 测试都显式带上 -c 指向它，
# 绝不读写用户真实的 config.json（用户的配置可能是真实的机器人 + 真实群号，
# 之前"就地修改再还原"的写法一旦中途出错就会破坏真实配置）。
TEST_CONFIG: Path | None = None
_TEST_CONFIG_PATH: Path | None = None


def run_cli(args: list[str], use_test_config: bool = True) -> tuple[int, str, str]:
    argv = list(args)
    # -c 是子命令的参数，必须放在子命令之后
    if use_test_config and TEST_CONFIG and "-c" not in argv and "--config" not in argv:
        for index, token in enumerate(argv):
            if token in ("draw", "stats", "fetch"):
                argv = argv[: index + 1] + ["-c", str(TEST_CONFIG)] + argv[index + 1 :]
                break
    out, err = io.StringIO(), io.StringIO()
    code = 0
    real_stderr = sys.stderr
    sys.stderr = err
    try:
        with redirect_stdout(out):
            code = cli_main(argv)
    except SystemExit as exc:  # argparse 退出
        code = int(exc.code or 0)
    finally:
        sys.stderr = real_stderr
    return code, out.getvalue(), err.getvalue()


def default_test_config() -> dict:
    """与出厂默认一致的离线配置，供各测试独立使用。"""
    return {
        "source": "file",
        "group_id": "",
        "days": 3,
        "winners": 1,
        "weighting": True,
        "weight_cap": 30,
        "min_messages": 1,
        "blacklist": [],
        "whitelist": [],
        "exclude_roles": [],
        "seed": None,
        "post_to_group": False,
        "history_file": "data/history.jsonl",
        "file": {"path": "data/messages.json", "format": "auto"},
        "onebot": {"base_url": "http://127.0.0.1:1", "access_token": ""},
        # 存档指向临时目录，绝不碰真实的 data/archive.db
        "archive": {"enabled": False, "path": "_unused.db", "auto_sync": False},
    }


def reset_test_config(**overrides) -> Path:
    """把隔离配置重置成默认值（可覆盖若干字段），并返回其路径。"""
    global TEST_CONFIG
    if TEST_CONFIG is None:
        if _TEST_CONFIG_PATH is None:
            raise RuntimeError("测试配置目录未初始化")
        TEST_CONFIG = _TEST_CONFIG_PATH
    data = default_test_config()
    data.update(overrides)
    TEST_CONFIG.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    return TEST_CONFIG


# ----------------------------------------------------------------- 工具函数测试


def test_helpers() -> None:
    section("工具函数")
    check("parse_time_window('3d') == 259200", parse_time_window("3d") == 259200)
    check("parse_time_window('72h') == 259200", parse_time_window("72h") == 259200)
    check("parse_time_window('3') 默认按天", parse_time_window("3") == 259200)
    check("parse_time_window('30m') == 1800", parse_time_window("30m") == 1800)
    check("非法时间窗口会报错", _raises(ConfigError, lambda: parse_time_window("3x")))
    check("to_user_id 统一成字符串", to_user_id(10001) == "10001" and to_user_id("10001") == "10001")
    check("to_user_id 处理浮点", to_user_id(10001.0) == "10001")


def _raises(exc_type, fn) -> bool:
    try:
        fn()
    except exc_type:
        return True
    except Exception:
        return False
    return False


# ----------------------------------------------------------------- 文件解析


def test_file_parsers() -> None:
    section("离线记录解析")
    data = ROOT / "data"
    expected_users = 17  # 13 名窗口内活跃 + 2 名潜水 + 1 个机器人 + 1 名只在一周前发言

    # 四种格式的样例文件
    sample_json = data / "sample_messages.json"
    sample_jsonl = data / "sample_messages.jsonl"
    sample_csv = data / "sample_messages.csv"
    sample_txt = data / "sample_messages.txt"
    default_json = data / "messages.json"

    for path in (sample_json, sample_jsonl, sample_csv, sample_txt, default_json):
        if not path.is_file():
            check(f"{path.name} 存在", False, "请先运行 scripts/make_demo_data.py")
            continue
        messages = load_from_file(path)
        users = {m.user_id for m in messages}
        check(f"{path.name} 解析出消息 (n={len(messages)})", len(messages) > 300)
        check(
            f"{path.name} 覆盖全部 {expected_users} 个 QQ 号 (实际 {len(users)})",
            len(users) == expected_users,
        )
        check(
            f"{path.name} 时间戳升序",
            all(a.timestamp <= b.timestamp for a, b in zip(messages, messages[1:])),
        )

    # 四种格式解析结果应当一致
    j = {(m.user_id, int(m.timestamp)) for m in load_from_file(sample_json)}
    c = {(m.user_id, int(m.timestamp)) for m in load_from_file(sample_csv)}
    t = {(m.user_id, int(m.timestamp)) for m in load_from_file(sample_txt)}
    d = {(m.user_id, int(m.timestamp)) for m in load_from_file(default_json)}
    check("JSON 与 CSV 解析一致", j == c, f"差异 {len(j ^ c)} 条")
    check("JSON 与 TXT 解析一致", j == t, f"差异 {len(j ^ t)} 条")
    check("JSON 与默认 data/messages.json 一致", j == d, f"差异 {len(j ^ d)} 条")

    # 昵称解析
    nick = {m.user_id: m.nickname for m in load_from_file(sample_json)}
    check("昵称解析正确", nick.get("10001") == "夜航船", f"实际 {nick.get('10001')!r}")


# ----------------------------------------------------------------- 抽奖规则


def _cfg(**kwargs) -> Config:
    base = dict(
        source="file",
        group_id="123456789",
        days=3,
        winners=1,
        weighting=True,
        history_file="",
    )
    base.update(kwargs)
    cfg = Config(**base)
    cfg.validate()
    return cfg


def test_rules() -> None:
    section("抽奖规则")
    messages = load_from_file(ROOT / "data" / "sample_messages.json")

    # 1. 窗口内候选 = 发过言的人，排除机器人
    cfg = _cfg(weight_cap=0)
    ctx = build_context(cfg, messages)
    ids = {a.user_id for a in ctx.candidates}
    bot_ids = {a.user_id for a in ctx.activities if looks_like_bot(a.user_id, a.nickname)}
    check("机器人被排除", "2854196310" not in ids)
    check("潜水员被排除", "10015" not in ids and "10016" not in ids)
    check(
        f"窗口内所有非机器人发言者都进入候选 ({len(ctx.activities) - len(bot_ids)} 人)",
        len(ctx.candidates) == len(ctx.activities) - len(bot_ids),
        f"实际 {len(ctx.candidates)}",
    )
    check("只统计窗口内消息", ctx.in_window_messages < ctx.total_messages)

    # 2. min_messages 门槛
    cfg2 = _cfg(min_messages=10)
    ctx2 = build_context(cfg2, messages)
    check(
        "min_messages=10 过滤掉发言不足的人",
        all(a.count >= 10 for a in ctx2.candidates) and len(ctx2.candidates) < len(ctx.candidates),
    )

    # 3. 黑名单
    cfg3 = _cfg(blacklist=["10001", "10002"])
    ctx3 = build_context(cfg3, messages)
    ids3 = {a.user_id for a in ctx3.candidates}
    check("黑名单生效", "10001" not in ids3 and "10002" not in ids3)
    check(
        "黑名单人数正确",
        len(ctx3.candidates) == len(ctx.candidates) - 2,
        f"实际 {len(ctx3.candidates)}",
    )
    check("黑名单排除原因有记录", any("黑名单" in r for r in ctx3.excluded))
    check("未发言的白名单/黑名单有提示", isinstance(ctx3.notes, list))

    # 4. 白名单
    cfg4 = _cfg(whitelist=["10001", "10002", "99999999"])
    ctx4 = build_context(cfg4, messages)
    check("白名单只保留名单内的人", {a.user_id for a in ctx4.candidates} == {"10001", "10002"})

    # 5. 身份排除（10004 是管理员）
    cfg5 = _cfg(exclude_roles=["admin"])
    ctx5 = build_context(cfg5, messages)
    check("排除管理员生效", "10004" not in {a.user_id for a in ctx5.candidates})
    cfg5b = _cfg(exclude_roles=["owner", "admin"])
    ctx5b = build_context(cfg5b, messages)
    check(
        "排除群主+管理员生效",
        not ({"10001", "10004"} & {a.user_id for a in ctx5b.candidates}),
    )

    # 6. 天数窗口
    ctx1 = build_context(_cfg(days=1), messages)
    ctx7 = build_context(_cfg(days=7), messages)
    check("窗口缩短后候选人数不增加", len(ctx1.candidates) <= len(ctx7.candidates))
    check("近 1 天窗口内人更少", len(ctx1.candidates) < 14, f"实际 {len(ctx1.candidates)}")
    # 7 天窗口应当把两名潜水员纳入（他们 4~10 天前发过言）
    check("近 7 天窗口纳入更多群友", len(ctx7.candidates) > len(ctx.candidates))

    # 7. 加权 vs 等概率
    cfg_w = _cfg(weight_cap=0)
    ctx_w = build_context(cfg_w, messages)
    check("加权权重等于发言条数", all(abs(a.weight_input - a.count) < 1e-9 for a in ctx_w.candidates))

    cfg_e = _cfg(weighting=False)
    ctx_e = build_context(cfg_e, messages)
    check("等概率模式权重全为 1", all(abs(a.weight_input - 1.0) < 1e-9 for a in ctx_e.candidates))

    # 8. weight_cap
    cfg_cap = _cfg(weight_cap=5)
    ctx_cap = build_context(cfg_cap, messages)
    check("weight_cap 生效", max(a.weight_input for a in ctx_cap.candidates) <= 5.0)
    check("weight_cap 有提示", any("权重上限" in n for n in ctx_cap.notes))

    # 8b. 默认不限制刷屏（weight_cap = 0）
    from qq_lottery.config import DEFAULTS

    check("默认不开启权重上限（0 = 不限制）", DEFAULTS["weight_cap"] == 0, str(DEFAULTS["weight_cap"]))
    cfg_nocap = _cfg(weight_cap=0)
    ctx_nocap = build_context(cfg_nocap, messages)
    check(
        "不限制时权重等于发言条数",
        all(abs(a.weight_input - a.count) < 1e-9 for a in ctx_nocap.candidates),
    )

    # 8e. 自动适配窗口（应对 NapCat 缓存只有固定条数、拿不到长历史的现实）
    now_auto = time.time()
    spread = [
        Message(user_id=str(i), nickname=f"u{i}", timestamp=now_auto - (600 - i * 60))
        for i in range(10)
    ]
    cfg_auto = _cfg(window_mode="auto")
    check("auto 模式被配置接受", cfg_auto.window_mode == "auto")
    from qq_lottery.lottery import resolve_window

    start_auto, end_auto, ref_auto = resolve_window(cfg_auto, spread, now=now_auto)
    check("auto 模式以最早一条消息为窗口起点", abs(start_auto - min(m.timestamp for m in spread)) < 1.0)
    check("auto 模式在参照说明里讲清楚了", "自动适配" in ref_auto, ref_auto[:60])
    ctx_auto = build_context(cfg_auto, spread, now=now_auto)
    check("auto 模式下数据覆盖完整", ctx_auto.coverage.get("complete") is True)
    check("auto 模式下候选纳入全部发言者", len(ctx_auto.candidates) == 10, str(len(ctx_auto.candidates)))

    cfg_days = _cfg(window_mode="days", days=3)
    ctx_days = build_context(cfg_days, spread, now=now_auto)
    check("days 模式在同批数据下覆盖不完整", ctx_days.coverage.get("complete") is False)
    check("days 模式窗口起点更早", ctx_days.window_start < ctx_auto.window_start)
    check("两种模式候选人数一致（数据相同）", len(ctx_days.candidates) == len(ctx_auto.candidates))

    # 分页拉取量：默认值要够大，否则 auto 窗口会被截断
    from qq_lottery.config import DEFAULTS as _D

    check("默认单页拉取量提高到 1000 条以上", _D["onebot"]["page_size"] >= 1000, str(_D["onebot"]["page_size"]))

    # 8f. 数据覆盖（公平性前提）
    from qq_lottery.lottery import fairness_report, window_coverage

    now_ts = time.time()
    shallow = [Message(user_id="9", nickname="刚说话的人", timestamp=now_ts - 60)]
    cov_shallow = window_coverage(shallow, now_ts - 3 * 86400, now_ts)
    check("窗口开头缺数据时判定为不完整", cov_shallow["complete"] is False)
    check("覆盖率被正确算出（不足 1%）", cov_shallow["coverage"] < 0.01, str(cov_shallow["coverage"]))
    check("给出了可读的缺口说明", "覆盖" in cov_shallow["message"] and "NapCat" in cov_shallow["message"])
    check("缺口时长被算出（约 72 小时）", 71 < cov_shallow["missing_seconds"] / 3600 < 73, str(cov_shallow["missing_seconds"] / 3600))

    full = [
        Message(user_id="9", nickname="a", timestamp=now_ts - 3 * 86400 + 60),
        Message(user_id="8", nickname="b", timestamp=now_ts - 60),
    ]
    cov_full = window_coverage(full, now_ts - 3 * 86400, now_ts)
    check("数据覆盖满窗口时判定为完整", cov_full["complete"] is True, cov_full["message"][:60])
    check("覆盖率为 100%", abs(cov_full["coverage"] - 1.0) < 1e-6, str(cov_full["coverage"]))

    # 末尾空档：机器人中途掉线，最近一段没收到
    lagging = [
        Message(user_id="9", nickname="a", timestamp=now_ts - 3 * 86400 + 60),
        Message(user_id="8", nickname="b", timestamp=now_ts - 6 * 3600),
    ]
    cov_lag = window_coverage(lagging, now_ts - 3 * 86400, now_ts)
    check("末尾缺数据时判定为不完整", cov_lag["complete"] is False)
    check("末尾缺口时长被算出（约 6 小时）", 5.9 < cov_lag["trailing_seconds"] / 3600 < 6.1, str(cov_lag["trailing_seconds"] / 3600))
    check("末尾缺数据时提示机器人中途停止", "中途停止" in cov_lag["message"] or "没有被采集到" in cov_lag["message"])

    cov_empty = window_coverage([], now_ts - 86400, now_ts)
    check("没有消息时覆盖率为 0", cov_empty["coverage"] == 0.0 and cov_empty["complete"] is False)

    ctx_shallow = build_context(_cfg(), shallow, now=now_ts)
    check("覆盖不足会出现在报告的提示里", any("覆盖" in n for n in ctx_shallow.notes))

    # 8d. 公平性记录：可复现、含权重与概率
    w1, ctx1, seed1 = run_draw(_cfg(seed=8888, winners=2), messages)
    rep = fairness_report(_cfg(seed=8888, winners=2), ctx1, w1, seed1, ctx1.coverage)
    check("公平性记录标明随机方式", "加权" in rep["method_label"] or "等概率" in rep["method_label"])
    check("公平性记录标明可复现", rep["reproducible"] is True and rep["seed"] == 8888)
    check("公平性记录含种子来源", "种子" in rep["seed_source"])
    check("公平性记录标明一人最多中一次", rep["dedup"] is True)
    check("公平性记录含每位中奖者的权重", len(rep["weights"]) == len(w1) and all("weight" in w for w in rep["weights"]))
    check("公平性记录含数据覆盖情况", "data_coverage" in rep and "coverage" in rep["data_coverage"])
    w2, _, _ = run_draw(_cfg(seed=8888, winners=2), messages)
    check("同种子可复现同一名单（公平性可核对）", [w.user_id for w in w1] == [w.user_id for w in w2])

    # 9. 多人抽取不重复
    cfg_multi = _cfg(winners=5)
    winners, ctx_multi, _ = run_draw(cfg_multi, messages)
    check("一次抽 5 人不重复", len({w.user_id for w in winners}) == 5 and len(winners) == 5)
    check("每轮中奖概率都在 0~1 之间", all(0.0 < w.probability <= 1.0 for w in winners))
    first = winners[0]
    total_weight = sum(a.weight_input for a in ctx_multi.candidates)
    expect_first = next(a for a in ctx_multi.candidates if a.user_id == first.user_id).weight_input / total_weight
    check(
        f"第一轮中奖概率等于权重占比（{first.probability:.4f} vs {expect_first:.4f}）",
        abs(first.probability - expect_first) < 1e-9,
    )
    check("中奖者的发言条数与统计一致", all(w.count >= 1 for w in winners))

    # 10. 抽取人数超过候选人数
    cfg_over = _cfg(winners=99)
    winners_over, ctx_over, _ = run_draw(cfg_over, messages)
    check(
        "抽取人数超过候选时只返回候选人数",
        len(winners_over) == len(ctx_over.candidates),
        f"{len(winners_over)} vs {len(ctx_over.candidates)}",
    )

    # 11. 种子可复现
    a, _, seed_a = run_draw(_cfg(seed=12345), messages)
    b, _, seed_b = run_draw(_cfg(seed=12345), messages)
    check("同种子结果可复现", [w.user_id for w in a] == [w.user_id for w in b] and seed_a == seed_b == 12345)

    # 12. 加权分布偏向活跃者（大样本统计检验）
    hot = "10001"  # 62 条
    cold = "10013"  # 2 条
    rng = random.Random(7)
    hot_wins = cold_wins = 0
    trials = 4000
    cand = ctx_w.candidates
    weights = {a.user_id: a.weight_input for a in cand}
    people = [a.user_id for a in cand]
    for _ in range(trials):
        total = sum(weights.values())
        pick, cursor = rng.random() * total, 0.0
        for uid in people:
            cursor += weights[uid]
            if pick <= cursor:
                hot_wins += uid == hot
                cold_wins += uid == cold
                break
    ratio = (hot_wins / max(cold_wins, 1))
    expected = weights[hot] / weights[cold]
    check(
        f"加权抽样频率比接近权重比（观测 {ratio:.1f}，理论 {expected:.1f}）",
        0.6 * expected < ratio < 1.6 * expected,
    )

    # 13. 时间窗口的边界处理
    now = time.time()
    mixed = [
        Message(user_id="1", nickname="活跃的人", timestamp=now - 3600),
        Message(user_id="2", nickname="很久没说话的人", timestamp=now - 10 * 86400),
    ]
    ctx_mixed = build_context(_cfg(), mixed, now=now)
    check(
        "窗口外的消息不计入统计",
        ctx_mixed.in_window_messages == 1 and len(ctx_mixed.candidates) == 1,
        f"in_window={ctx_mixed.in_window_messages} candidates={len(ctx_mixed.candidates)}",
    )

    # 整段记录都很旧时，窗口自动锚定到最近一条消息，而不是算出空名单
    stale = [Message(user_id="2", nickname="很久没说话的人", timestamp=now - 10 * 86400)]
    ctx_stale = build_context(_cfg(), stale, now=now)
    check(
        "记录过旧时窗口锚定到最新消息",
        ctx_stale.in_window_messages == 1 and abs(ctx_stale.window_end - (now - 10 * 86400)) < 1.0,
        f"end={ctx_stale.window_end} expect={now - 10 * 86400}",
    )
    check("锚定窗口说明出现在 context.reference 里", "最近一条消息" in ctx_stale.reference)

    winners_stale, _, _ = run_draw(_cfg(), stale, now=now)
    check("只有一个候选时必定中奖", len(winners_stale) == 1 and winners_stale[0].user_id == "2")

    ctx_empty = build_context(_cfg(), [], now=now)
    check("完全没有消息时也有提示", any("没有读取到任何消息" in n for n in ctx_empty.notes))
    winners_empty, _, _ = run_draw(_cfg(), [], now=now)
    check("没有任何消息时返回空名单", winners_empty == [])

    # 数据是新鲜的（最新消息就在刚才）时，窗口终点应等于当前时间
    fresh = [Message(user_id="3", nickname="刚说话的人", timestamp=now - 60)]
    ctx_fresh = build_context(_cfg(), fresh, now=now)
    check("数据新鲜时窗口终点为当前时间", abs(ctx_fresh.window_end - now) < 1.0)
    check("数据新鲜时刚好落在窗口外的人被排除", len(ctx_fresh.candidates) == 1)

    # 窗口极短（1 秒）时会真的出现「窗口内没有消息」
    tiny = _cfg(days=1 / 86400)
    ctx_tiny = build_context(tiny, fresh, now=now)
    check(
        "极短窗口下窗口内无人时给出提示",
        ctx_tiny.in_window_messages == 0 and any("没有任何消息" in n for n in ctx_tiny.notes),
        f"in_window={ctx_tiny.in_window_messages} notes={ctx_tiny.notes}",
    )


# ----------------------------------------------------------------- OneBot 接口


class MockOneBot(BaseHTTPRequestHandler):
    """模拟 OneBot v11：支持翻页、token 校验、send_group_msg。"""

    now = time.time()
    total = 500
    page_size = 100
    token = "secret-token"
    sent: list[dict] = []
    auth_failures = 0

    def log_message(self, *args):  # 静默
        pass

    def _reply(self, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self) -> None:  # noqa: N802
        action = self.path.strip("/").split("/")[-1]
        length = int(self.headers.get("Content-Length") or 0)
        params = json.loads(self.rfile.read(length) or b"{}")
        auth = self.headers.get("Authorization") or ""

        if action == "get_login_info":
            if auth != f"Bearer {MockOneBot.token}":
                MockOneBot.auth_failures += 1
            return self._reply({"status": "ok", "retcode": 0, "data": {"user_id": 3000000001, "nickname": "bot"}})

        if action == "get_group_msg_history":
            seq = params.get("message_seq")
            count = int(params.get("count") or MockOneBot.page_size)
            # message_seq 越大越新；未指定则从最新一页开始
            newest = MockOneBot.total if seq is None else int(seq) - 1
            oldest = max(1, newest - count + 1)
            rows = []
            for i in range(newest, oldest - 1, -1):
                rows.append(
                    {
                        "group_id": 123456789,
                        "message_id": 900000 + i,
                        "message_seq": i,
                        "user_id": 20000 + (i % 7),
                        "time": int(MockOneBot.now - (MockOneBot.total - i) * 600),
                        "sender": {"user_id": 20000 + (i % 7), "nickname": f"成员{i % 7}", "role": "member"},
                    }
                )
            return self._reply({"status": "ok", "retcode": 0, "data": {"messages": rows}})

        if action == "send_group_msg":
            MockOneBot.sent.append(params)
            return self._reply({"status": "ok", "retcode": 0, "data": {"message_id": 555}})

        return self._reply({"status": "failed", "retcode": 1404, "message": "unknown action"})


def start_mock_server() -> tuple[HTTPServer, int, threading.Thread]:
    server = HTTPServer(("127.0.0.1", 0), MockOneBot)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return server, server.server_address[1], thread


def test_onebot() -> None:
    reset_test_config()
    section("OneBot 机器人接口")
    server, port, _ = start_mock_server()
    base = f"http://127.0.0.1:{port}"
    try:
        cfg = _cfg(source="onebot", weight_cap=0)
        cfg.onebot_base_url = base
        cfg.onebot_access_token = MockOneBot.token
        cfg.onebot_page_size = 100
        cfg.onebot_max_pages = 20
        cfg.onebot_fetch_limit = 10000

        since = MockOneBot.now - 3 * 86400
        messages = load_from_onebot(cfg, since=since)
        users = {m.user_id for m in messages}
        check(f"翻页拉取到消息 (n={len(messages)})", len(messages) > 100)
        check(f"覆盖全部 7 名成员 (实际 {len(users)})", len(users) == 7)
        check("拉取结果按时间升序", all(a.timestamp <= b.timestamp for a, b in zip(messages, messages[1:])))
        check("翻页在窗口边界停止", min(m.timestamp for m in messages) < since + 600 * 2)
        check("机器人自身消息被排除", "3000000001" not in users)
        check("token 已携带", MockOneBot.auth_failures == 0)

        # 无 token 时服务端仍返回数据，但客户端应能正常工作（token 可选）
        cfg2 = _cfg(source="onebot")
        cfg2.onebot_base_url = base
        cfg2.onebot_page_size = 100
        messages2 = load_from_onebot(cfg2, since=since)
        check("未配置 token 也能拉取", len(messages2) > 0)

        # 连接失败要有可读报错
        from qq_lottery.sources import OneBotClient, OneBotError

        try:
            OneBotClient("http://127.0.0.1:1", "", timeout=2).get_group_msg_history("123456789")
            check("连接失败有友好报错", False, "没有抛出异常")
        except OneBotError as exc:
            check("连接失败有友好报错", "无法连接" in str(exc) or "接口" in str(exc), str(exc))

        # CLI 全流程：拉取 -> 抽奖 -> 发群
        cfg_path = ROOT / "data" / "test_onebot_config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "source": "onebot",
                    "group_id": "123456789",
                    "days": 3,
                    "winners": 2,
                    "weighting": True,
                    "history_file": "",
                    "onebot": {
                        "base_url": base,
                        "access_token": MockOneBot.token,
                        "page_size": 100,
                        "max_pages": 20,
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        # 先确认安全锁拦得住（防止测试消息进真实群）
        code, out, err = run_cli(
            ["draw", "-c", str(cfg_path), "--seed", "42", "--post", "--no-history"]
        )
        check(
            "测试环境下 --post 被安全锁拦住",
            code != 0 and "QQLOTTERY_NO_SEND" in err,
            f"code={code} err={err[:80]!r}",
        )
        check("被拦住时没有真的发出消息", len(MockOneBot.sent) == 0, str(len(MockOneBot.sent)))

        # 再显式临时解锁，验证 CLI 的发送链路本身可用。
        # 目标只是本进程内的模拟服务器（127.0.0.1 随机端口），不指向真实 NapCat。
        os.environ.pop("QQLOTTERY_NO_SEND", None)
        try:
            code, out, err = run_cli(
                ["draw", "-c", str(cfg_path), "--seed", "42", "--post", "--no-history",
                 "--json", str(ROOT / "data" / "test_result.json")]
            )
            check("临时解锁后 CLI 抽奖退出码为 0", code == 0, err)
            check("CLI 输出了中奖名单", "中奖名单" in out)
            check("结果已发送到群", len(MockOneBot.sent) == 1, f"实际 {len(MockOneBot.sent)} 次")
            if MockOneBot.sent:
                text = MockOneBot.sent[0].get("message", "")
                check("群消息包含开奖信息", "开奖" in text and "近 3 天" in text)
                check("群消息里是配置的群号", MockOneBot.sent[0].get("group_id") == 123456789)
        finally:
            os.environ["QQLOTTERY_NO_SEND"] = "1"
        check("安全锁已恢复", os.environ.get("QQLOTTERY_NO_SEND") == "1")
        result = json.loads((ROOT / "data" / "test_result.json").read_text(encoding="utf-8"))
        check("JSON 结果字段完整", {"winners", "candidate_count", "window", "seed"} <= set(result))
        check("JSON 记录了种子", result["seed"] == 42)
        check("JSON 中奖人数为 2", len(result["winners"]) == 2)
    finally:
        server.shutdown()
        server.server_close()


# ----------------------------------------------------------------- CLI 输出


def test_cli_outputs() -> None:
    reset_test_config()
    section("CLI 输出与文件")
    data = ROOT / "data"
    demo = data / "sample_messages.json"

    code, out, err = run_cli(["draw", "--source", "file", "--file", str(demo), "-n", "3", "--top", "5", "--no-history"])
    check("draw 退出码 0", code == 0, err)
    check("draw 输出窗口说明", "近 3 天" in out)
    check("draw 输出候选人数", "候选人数" in out)
    check("draw 输出活跃排行", "活跃度排行" in out)
    check("draw 输出排除原因", "疑似机器人" in out)
    cand_match = re.search(r"候选人数\s*:\s*(\d+) 人", out)
    bot_line = re.search(r"疑似机器人账号：([^\n]+)", out)
    check("draw 输出了候选人数", cand_match is not None)
    check(
        "机器人被单独列在排除区",
        bot_line is not None and "2854196310" in bot_line.group(1),
        f"实际 {(bot_line.group(1) if bot_line else None)!r}",
    )
    winner_block = out.split("🎉 中奖名单")[1].split("活跃度排行")[0] if "🎉 中奖名单" in out else ""
    check("机器人没有出现在中奖名单里", "2854196310" not in winner_block)
    if cand_match:
        check(
            "候选人数与统计一致",
            int(cand_match.group(1)) == len(build_context(Config(source="file", days=3, history_file=""), load_from_file(demo)).candidates),
        )

    winners_lines = [ln for ln in out.splitlines() if re.match(r"\s*\[\d+\] ", ln)]
    check("中奖名单为 3 人", len(winners_lines) == 3, f"实际 {len(winners_lines)}")

    # quiet 模式便于脚本调用
    code, out, err = run_cli(["draw", "--source", "file", "--file", str(demo), "-n", "2", "-q", "--no-history"])
    rows = [ln for ln in out.splitlines() if ln.strip() and not ln.startswith("（")]
    check("quiet 模式每行一条结果", len(rows) == 2 and all("\t" in r for r in rows))
    check("quiet 模式不输出装饰框", "====" not in out)

    # CSV 导出
    csv_path = data / "test_winners.csv"
    code, _, err = run_cli(
        ["draw", "--source", "file", "--file", str(demo), "-n", "3", "--csv", str(csv_path), "--no-history"]
    )
    check("CSV 导出成功", code == 0 and csv_path.is_file(), err)
    if csv_path.is_file():
        with csv_path.open(encoding="utf-8-sig", newline="") as fh:
            rows = list(csv.reader(fh))
        check("CSV 有表头", rows[0][:3] == ["名次", "QQ号", "昵称"])
        check("CSV 行数正确", len(rows) == 4, f"实际 {len(rows)}")

    # JSON 输出到 stdout
    code, out, err = run_cli(["draw", "--source", "file", "--file", str(demo), "-n", "2", "--json", "--no-history", "-q"])
    payload = None
    for chunk in re.findall(r"\{.*\}", out, re.S):
        try:
            payload = json.loads(chunk)
            break
        except json.JSONDecodeError:
            continue
    check("--json 输出合法 JSON", payload is not None)
    if payload:
        expected_candidates = len(build_context(Config(source="file", days=3, history_file=""), load_from_file(demo)).candidates)
        check(
            f"JSON candidate_count 与上下文一致 ({expected_candidates})",
            payload["candidate_count"] == expected_candidates,
            f"实际 {payload['candidate_count']}",
        )

    # stats
    code, out, err = run_cli(["stats", "--source", "file", "--file", str(demo), "--top", "5"])
    check("stats 退出码 0", code == 0, err)
    check("stats 输出活跃榜", "活跃度统计" in out and "夜航船" in out)

    # fetch 不支持 file 源（应当报错退出）
    code, out, err = run_cli(["fetch", "--source", "file", "--file", str(demo), "-o", str(data / "x.json")])
    check("fetch 需要 onebot 数据源时报错", code != 0)

    # 缺参数时的报错
    code, out, err = run_cli(["draw", "--source", "onebot", "--no-history"])
    check("onebot 缺群号时报配置错误", code == 2 and "群号" in err, f"code={code} err={err!r}")

    code, out, err = run_cli(["draw", "--source", "file", "--file", str(data / "nonexistent.json")])
    check("文件不存在时报错", code == 2 and "不存在" in err, f"code={code}")

    # 历史记录
    hist = data / "test_history.jsonl"
    hist.unlink(missing_ok=True)
    code, _, err = run_cli(
        ["draw", "--source", "file", "--file", str(demo), "-n", "1", "--history", str(hist)]
    )
    check("历史记录已写入", code == 0 and hist.is_file())
    if hist.is_file():
        lines = [ln for ln in hist.read_text(encoding="utf-8").splitlines() if ln.strip()]
        check("历史记录是一行一条 JSON", len(lines) == 1 and json.loads(lines[0])["winners"])

    # 参数校验
    code, _, err = run_cli(["draw", "-n", "0"])
    check("-n 0 被拒绝", code == 2)
    code, _, err = run_cli(["draw", "--days", "abc"])
    check("--days abc 被拒绝", code == 2)

    # 时间窗口参数
    code, out, err = run_cli(
        ["draw", "--source", "file", "--file", str(demo), "--days", "72h", "--no-history", "-q"]
    )
    check("--days 72h 等价于 3 天", code == 0 and "（无中奖者）" not in out, err)


def test_config_parsing() -> None:
    section("配置文件")
    cfg_path = ROOT / "config.json"
    cfg = Config.load(cfg_path)
    check("读取 config.json 成功", cfg.days == 3)
    check("config.json 中 _ 注释被忽略", cfg.group_id == "" or cfg.group_id.isdigit())

    # 命令行覆盖配置文件
    cfg2 = Config.load(cfg_path, {"days": 1, "winners": 5, "blacklist": ["10001"]})
    check("命令行覆盖 days", cfg2.days == 1)
    check("命令行覆盖 winners", cfg2.winners == 5)
    check("命令行覆盖 blacklist", cfg2.blacklist == ["10001"])

    bad = ROOT / "data" / "test_bad_config.json"
    bad.write_text("{ this is not json", encoding="utf-8")
    check("非法 JSON 配置有可读报错", _raises(ConfigError, lambda: Config.load(bad)))


def cleanup_artifacts() -> int:
    """清理自检过程中写出的临时文件，避免污染 data/ 与 out/。"""
    removed = 0
    patterns = ("test_*.json", "test_*.csv", "test_*.jsonl", "x.json", "history.jsonl")
    for pattern in patterns:
        for item in (ROOT / "data").glob(pattern):
            if item.is_file():
                item.unlink()
                removed += 1
    for folder, pattern in (("out", "*"), ("data", "demo_*")):
        target = ROOT / folder
        if target.is_dir():
            for item in target.glob(pattern):
                if item.is_file():
                    item.unlink()
                    removed += 1
    for cache in ROOT.rglob("__pycache__"):
        shutil.rmtree(cache, ignore_errors=True)
    return removed


def main() -> int:
    global TEST_CONFIG, _TEST_CONFIG_PATH

    _setup_console()
    print("QQ 群抽奖工具 · 自检开始")
    if not (ROOT / "data" / "sample_messages.json").is_file():
        print("缺少演示数据，请先运行: python scripts/make_demo_data.py")
        return 1

    tmp_dir = Path(tempfile.mkdtemp(prefix="qq-lottery-selftest-"))
    _TEST_CONFIG_PATH = tmp_dir / "config.json"
    TEST_CONFIG = _TEST_CONFIG_PATH
    reset_test_config()
    print(f"隔离配置：{TEST_CONFIG}")
    print("（你真实的 config.json 不会被读写）")

    try:
        test_helpers()
        test_file_parsers()
        test_rules()
        test_onebot()
        test_cli_outputs()
        test_config_parsing()
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        removed = cleanup_artifacts()
        if removed:
            print(f"\n已清理 {removed} 个测试产物")
        TEST_CONFIG = None

    print("\n" + "=" * 50)
    print(f"通过 {len(PASSED)} 项，失败 {len(FAILED)} 项")
    if FAILED:
        for name in FAILED:
            print(f"  [FAIL] {name}")
        return 1
    print("全部通过 ✅")
    return 0


if __name__ == "__main__":
    sys.exit(main())
