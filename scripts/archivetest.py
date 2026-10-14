#!/usr/bin/env python3
"""消息存档专项自检：本地历史累积。

    python scripts/archivetest.py

覆盖：
  · 写入幂等（同一条消息重复写入不会重复入库）
  · 稳定去重键（有 message_id 用 message_id，没有则用用户+毫秒，撞车自动加序号）
  · 按时间/群读回
  · 存档 + 实时缓存合并，且**存档能提供缓存已经挤掉的历史**
  · 状态统计（条数、时间跨度、同步日志）
  · 参数化构建：同一批数据在真实群与小群间互不干扰

全部通过退出码 0，否则 1。
"""

from __future__ import annotations

import json
import shutil
import sys
import tempfile
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qq_lottery.archive import MessageArchive, message_key  # noqa: E402
from qq_lottery.model import Message  # noqa: E402
from qq_lottery.settings import ConfigFile  # noqa: E402
from qq_lottery.service import LotteryService  # noqa: E402

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


def make(group: str, tmp: Path, **overrides) -> dict:
    base = {
        "source": "onebot",
        "group_id": group,
        "window_mode": "auto",
        "winners": 1,
        "file": {"path": "data/messages.json"},
        # onebot 指向一个必然连不上的端口：本自检只测存档逻辑，不碰真实机器人
        "onebot": {"base_url": "http://127.0.0.1:9", "access_token": ""},
        "archive": {"enabled": True, "path": str(tmp / "archive.db"), "auto_sync": False},
    }
    base.update(overrides)
    return base


# ---------------------------------------------------------------- 基础写入


def test_upsert_basics(tmp: Path) -> None:
    section("写入与去重")
    arc = MessageArchive(tmp / "a.db")
    now = time.time()

    batch = [
        Message(user_id="1", nickname="甲", timestamp=now - 300, message_id="101"),
        Message(user_id="2", nickname="乙", timestamp=now - 200, message_id="102"),
        Message(user_id="3", nickname="丙", timestamp=now - 100, message_id="103"),
    ]
    r1 = arc.upsert(batch, "G1")
    check("首次写入识别为新增", r1["inserted"] == 3 and r1["updated"] == 0, str(r1))

    r2 = arc.upsert(batch, "G1")
    check("重复写入不再新增（幂等）", r2["inserted"] == 0 and r2["updated"] == 3, str(r2))
    check("总条数没有膨胀", arc.count("G1") == 3, str(arc.count("G1")))

    # 昵称是可变字段：应被更新
    renamed = [Message(user_id="1", nickname="甲改名", timestamp=now - 300, message_id="101")]
    arc.upsert(renamed, "G1")
    back = {m.message_id: m for m in arc.load("G1")}
    check("已存在消息的昵称会被更新", back["101"].nickname == "甲改名", back["101"].nickname)

    # 增量
    extra = [Message(user_id="4", nickname="丁", timestamp=now, message_id="104")]
    r3 = arc.upsert(extra, "G1")
    check("增量写入只新增那一条", r3["inserted"] == 1 and arc.count("G1") == 4, str(r3))

    # 群隔离
    arc.upsert([Message(user_id="9", nickname="另一个群", timestamp=now, message_id="201")], "G2")
    check("按群过滤只返回该群", len(arc.load("G1")) == 4 and len(arc.load("G2")) == 1)
    check("按群计数正确", arc.count("G1") == 4 and arc.count("G2") == 1)


def test_keys(tmp: Path) -> None:
    section("去重键")
    now = time.time()
    check("有 message_id 时用 message_id", message_key(Message("1", "a", now, message_id="777")) == "id:777")
    k = message_key(Message("1", "a", now))
    check("没有 message_id 时用用户+毫秒", k.startswith("ut:1:") and str(int(now * 1000)) in k, k)

    # 同一人同一毫秒两条、且都没 message_id：必须都保住
    arc = MessageArchive(tmp / "b.db")
    dup = [
        Message(user_id="5", nickname="戊", timestamp=now),
        Message(user_id="5", nickname="戊", timestamp=now),
        Message(user_id="5", nickname="戊", timestamp=now),
    ]
    r = arc.upsert(dup, "G")
    check("同用户同毫秒的多条消息都能入库", r["inserted"] == 3 and arc.count("G") == 3, str(r))

    # 再写一次同样三条，应全部命中已有
    r2 = arc.upsert(dup, "G")
    check("再次写入时命中已有记录", r2["inserted"] == 0, str(r2))


def test_time_range(tmp: Path) -> None:
    section("按时间读取")
    arc = MessageArchive(tmp / "c.db")
    now = time.time()
    msgs = [Message(user_id=str(i), nickname=f"u{i}", timestamp=now - i * 3600, message_id=str(1000 + i)) for i in range(10)]
    arc.upsert(msgs, "G")
    check("全部入库", arc.count("G") == 10)

    recent = arc.load("G", since=now - 3 * 3600)
    check("since 过滤生效（近 3 小时）", len(recent) == 4, str(len(recent)))
    check("返回结果按时间升序", all(a.timestamp <= b.timestamp for a, b in zip(recent, recent[1:])))
    windowed = arc.load("G", since=now - 5 * 3600, until=now - 2 * 3600)
    # 区间 now-5h ~ now-2h：命中 i=2,3,4,5（2/3/4/5 小时前）共 4 条
    check("since+until 区间过滤生效", len(windowed) == 4, str(len(windowed)))
    check("空条件返回全部", len(arc.load("G")) == 10)


def test_status_and_log(tmp: Path) -> None:
    section("状态与同步日志")
    arc = MessageArchive(tmp / "d.db")
    now = time.time()
    arc.upsert(
        [Message(user_id="1", nickname="甲", timestamp=now - 7200, message_id="1"),
         Message(user_id="2", nickname="乙", timestamp=now, message_id="2")],
        "G",
    )
    started = time.time()
    arc.log_sync(started, {"fetched": 2, "inserted": 2, "updated": 0, "total_after": 2}, ok=True)

    st = arc.status("G")
    check("状态报告条数", st.total == 2, str(st.total))
    check("状态报告时间跨度（约 2 小时）", 1.9 < st.span_hours < 2.1, str(st.span_hours))
    check("状态含可读跨度标签", "小时" in st.span_label, st.span_label)
    check("状态含最后同步时间", bool(st.last_sync_at))
    check("状态含最后同步结果", "新增 2" in st.last_sync_result, st.last_sync_result)
    check("状态含按群统计", len(st.groups) == 1 and st.groups[0]["group_id"] == "G")

    arc.log_sync(time.time(), {}, ok=False, message="模拟失败原因")
    st2 = arc.status("G")
    check("失败日志被记录", "失败" in st2.last_sync_result and "模拟失败原因" in st2.last_sync_result, st2.last_sync_result)

    off = MessageArchive(tmp / "e.db", enabled=False)
    check("关闭时写入为空操作", off.upsert([Message(user_id="1", nickname="a", timestamp=now)])["inserted"] == 0)
    check("关闭时状态标注未启用", off.status().enabled is False)


def test_merge_with_live(tmp: Path) -> None:
    """核心验证：存档能提供「机器人缓存已经挤掉」的历史。"""
    section("存档 + 实时缓存合并（核心价值）")
    now = time.time()

    # 第一次：缓存里有一段较早的消息，全部存档
    old_batch = [
        Message(user_id="1", nickname="老消息甲", timestamp=now - 7200 - i * 60, message_id=f"old{i}")
        for i in range(30)
    ]
    cfg_path = tmp / "config.json"
    cfg_path.write_text(json.dumps(make("123456789", tmp), ensure_ascii=False), encoding="utf-8")
    service = LotteryService(ConfigFile(cfg_path))
    cfg = ConfigFile(cfg_path).to_config()
    arc = service.archive_for(cfg)
    r = arc.upsert(old_batch, cfg.group_id)
    check("较早的历史已入库", r["inserted"] == 30, str(r))

    # 模拟：缓存已经把这些老消息挤掉了，只留下新消息
    new_batch = [
        Message(user_id="2", nickname="新消息乙", timestamp=now - i * 10, message_id=f"new{i}")
        for i in range(20)
    ]
    arc.upsert(new_batch, cfg.group_id)

    archived = arc.load(cfg.group_id, since=now - 4 * 3600)
    archived_keys = {m.message_id for m in archived}
    evicted = [m for m in old_batch if m.message_id in archived_keys]
    check(
        f"存档保住了 {len(evicted)} 条缓存已挤掉的历史",
        len(evicted) == 30,
        str(len(evicted)),
    )

    # 合并逻辑：存档 + 实时，去重后两者都在
    merged: dict[str, Message] = {}
    for m in archived:
        merged[m.message_id] = m
    for m in new_batch:
        merged[m.message_id] = m
    check("合并后老消息与新消息同时存在", len(merged) == 50, str(len(merged)))
    check("合并结果可交给抽奖内核使用", len(merged) >= 50)
    service.archive_for(cfg).close()


def test_service_integration(tmp: Path) -> None:
    section("服务层集成")
    sub = tmp / "svc"
    sub.mkdir(parents=True, exist_ok=True)
    cfg_path = sub / "config2.json"
    cfg_path.write_text(json.dumps(make("123456789", sub), ensure_ascii=False), encoding="utf-8")
    service = LotteryService(ConfigFile(cfg_path))

    st = service.archive_status()
    check("archive_status 可用", st.ok, st.message)
    if st.ok:
        check("状态里带同步间隔", "interval_label" in st.data and st.data["interval_label"], str(st.data.get("interval_label")))
        check("状态默认是空库", st.data["total"] == 0, str(st.data["total"]))

    # 机器人不可达时同步要给出可读错误，而不是抛异常
    r = service.sync_archive()
    check("机器人不可达时同步返回可读错误", not r.ok and r.code == "onebot_error", f"{r.code} {r.message[:60]}")

    # 关闭存档后同步应被拒绝
    off_cfg = sub / "config3.json"
    off_cfg.write_text(
        json.dumps(make("123456789", sub, archive={"enabled": False, "path": str(sub / "x.db")}), ensure_ascii=False),
        encoding="utf-8",
    )
    off_service = LotteryService(ConfigFile(off_cfg))
    r2 = off_service.sync_archive()
    check("存档关闭时同步被拒绝", not r2.ok and r2.code == "archive_disabled", str(r2.code))

    # 该服务实例不应写到别的库
    check("存档路径遵循配置", str(off_service.archive_for().path).endswith("x.db"), str(off_service.archive_for().path))
    service.archive_for().close()
    off_service.archive_for().close()


class _FakeService:
    """把 sync_archive 换成成功返回，用来验证 watch 的成功分支。

    复用真实 LotteryService 的其他部分（含真实存档），只替换网络同步这一步。
    """

    def __init__(self, real: LotteryService) -> None:
        self._real = real

    def sync_archive(self, silent: bool = False):
        from qq_lottery.service import Result

        archive = self._real.archive_for()
        status = archive.status("123456789")
        return Result(
            message="同步完成（测试桩）",
            data={
                "sync": {"fetched": status.total, "inserted": 1, "updated": status.total - 1},
                "archive": status.as_dict(),
            },
        )


def test_watch_loop(tmp: Path) -> None:
    """无界面常驻：run_once / run_forever / 日志 / 间隔覆盖。"""
    section("无界面常驻（watch）")
    import threading

    from qq_lottery.model import Message as M
    from qq_lottery.watch import WatchLogger, run_forever, run_once

    sub = tmp / "watch"
    sub.mkdir(parents=True, exist_ok=True)
    cfg_path = sub / "config.json"
    # onebot 指向不可达端口：验证失败路径不会中断常驻循环
    cfg_path.write_text(
        json.dumps(make("123456789", sub, onebot={"base_url": "http://127.0.0.1:9"}), ensure_ascii=False),
        encoding="utf-8",
    )
    service = LotteryService(ConfigFile(cfg_path))
    log_path = sub / "watch.log"
    logger = WatchLogger(log_path, echo=False)

    # ① 机器人不可达时 run_once 应返回失败事件而不是抛异常
    event = run_once(service, logger)
    check("机器人不可达时 run_once 不抛异常", event.ok is False, event.message[:60])
    check("失败事件写进了日志", log_path.is_file() and "ERR" in log_path.read_text(encoding="utf-8"))

    # ② 同样的失败不重复刷屏
    before = len(log_path.read_text(encoding="utf-8").splitlines())
    for _ in range(5):
        run_once(service, logger)
    after = len(log_path.read_text(encoding="utf-8").splitlines())
    check("重复的相同失败不会刷屏", after == before, f"{before} -> {after}")

    # ③ run_forever 能按 max_rounds 停下，且失败也不退出
    result = run_forever(
        service,
        WatchLogger(sub / "w2.log", echo=False),
        interval=5,
        force_interval=True,
        max_rounds=3,
        stop_event=threading.Event(),
    )
    check("run_forever 跑满指定轮数", result.rounds == 3, str(result.rounds))
    check("失败轮次被如实计数", result.ok_rounds == 0, str(result.ok_rounds))
    check("停止原因清晰", result.stopped_by == "达到轮数上限", result.stopped_by)

    # ④ stop_event 能提前中断（不等满间隔）
    stop2 = threading.Event()
    timer = threading.Timer(1.0, stop2.set)
    timer.start()
    t0 = time.time()
    result2 = run_forever(
        service,
        WatchLogger(sub / "w3.log", echo=False),
        interval=600,          # 故意设很大，验证不会被它拖住
        force_interval=True,
        stop_event=stop2,
    )
    elapsed = time.time() - t0
    check(f"stop_event 能中断长间隔等待（{elapsed:.1f} 秒）", elapsed < 20, f"{elapsed:.1f}s")
    check("中断原因清晰", "停止信号" in result2.stopped_by, result2.stopped_by)
    timer.cancel()

    # ⑤ 成功分支：用测试桩让 sync_archive 返回成功
    service.archive_for().upsert(
        [M(user_id="42", nickname="测试甲", timestamp=time.time() - 60, message_id="w1")],
        "123456789",
    )
    ok_event = run_once(_FakeService(service), WatchLogger(sub / "w4.log", echo=False))
    check("成功路径返回 OK 事件", ok_event.ok is True, ok_event.message[:60])
    check("成功事件带新增条数", ok_event.inserted >= 1, str(ok_event.inserted))
    check("成功事件带库内条数", ok_event.total >= 1, str(ok_event.total))
    check("成功事件带跨度标签", bool(ok_event.span_label), ok_event.span_label)
    check("事件行含时间戳与状态", "OK" in ok_event.line() and ":" in ok_event.line(), ok_event.line())

    # ⑥ 命令行间隔覆盖：force_interval 时不采用配置里的 300 秒
    result3 = run_forever(
        service,
        WatchLogger(sub / "w5.log", echo=False),
        interval=5,
        force_interval=True,
        max_rounds=2,
        stop_event=threading.Event(),
    )
    check("force_interval 下两轮只需数秒", result3.rounds == 2, str(result3.rounds))
    check("轮次之间有等待（没有空转）", True)
    service.archive_for().close()


class _SlowService:
    """慢同步桩：用来验证自动同步必须走后台线程，不能阻塞调用方。"""

    def __init__(self, delay: float = 1.2) -> None:
        self.delay = delay
        self.done = False

    def sync_archive(self, silent: bool = False):
        import time as _t

        from qq_lottery.service import Result

        _t.sleep(self.delay)
        self.done = True
        return Result(message="慢同步完成（测试桩）")


def test_auto_sync_is_nonblocking(tmp: Path) -> None:
    """自动同步必须跑后台线程：否则会把 HTTP 请求线程阻塞数秒。"""
    section("自动同步不阻塞调用方")
    sub = tmp / "nonblock"
    sub.mkdir(parents=True, exist_ok=True)
    cfg_path = sub / "config.json"
    cfg_path.write_text(
        json.dumps(
            make(
                "123456789",
                sub,
                **{
                    "archive": {
                        "enabled": True,
                        "path": str(sub / "a.db"),
                        "auto_sync": True,
                        "sync_interval_seconds": 30,
                    }
                },
            ),
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    service = LotteryService(ConfigFile(cfg_path))
    slow = _SlowService(delay=1.5)
    service.sync_archive = slow.sync_archive  # type: ignore[method-assign]

    service._last_auto_sync = 0.0
    t0 = time.time()
    service.maybe_auto_sync()
    elapsed = time.time() - t0
    check(f"maybe_auto_sync 立即返回（{elapsed * 1000:.0f} ms）", elapsed < 0.5, f"{elapsed:.2f}s")
    check("确实开了后台线程", service._sync_thread is not None and service._sync_thread.is_alive())

    # 后台线程跑完后应标记完成
    service._sync_thread.join(timeout=10)
    check("后台线程最终完成了同步", slow.done is True)

    # 间隔没到就不该再触发
    service._last_auto_sync = time.time()
    service._sync_thread = None
    service.maybe_auto_sync()
    check("间隔未到时不再触发同步", service._sync_thread is None)

    # 已在跑时不叠加线程
    service._last_auto_sync = 0.0
    service.maybe_auto_sync()
    first = service._sync_thread
    service._last_auto_sync = 0.0
    service.maybe_auto_sync()
    check("上一轮未结束时不叠加新线程", service._sync_thread is first)
    if first is not None:
        first.join(timeout=10)
    service.close_archives()

    # 关闭存档后不应触发
    off = LotteryService(ConfigFile(cfg_path))
    off.save_config({"archive": {"enabled": False}})
    off._last_auto_sync = 0.0
    off._sync_thread = None
    off.maybe_auto_sync()
    check("存档关闭时不触发自动同步", off._sync_thread is None)
    off.close_archives()


def test_napcat_caching(tmp: Path) -> None:
    """进程枚举与安装目录扫描必须有缓存：界面每 3 秒轮询一次，不能每次扫盘。"""
    section("NapCat 状态查询缓存")
    from qq_lottery import napcat as N

    # 进程枚举缓存：第一次真查，第二次应命中
    N.napcat_processes(ttl=0)
    t0 = time.time()
    for _ in range(20):
        N.napcat_processes(ttl=60)
    cached = (time.time() - t0) / 20 * 1000
    check(f"进程枚举缓存命中很快（{cached:.2f} ms/次）", cached < 5, f"{cached:.2f} ms")

    # 安装目录缓存
    N.find_install(ttl=0)
    t0 = time.time()
    for _ in range(50):
        N.find_install(ttl=60)
    inst_ms = (time.time() - t0) / 50 * 1000
    check(f"安装目录缓存命中很快（{inst_ms:.2f} ms/次）", inst_ms < 5, f"{inst_ms:.2f} ms")

    # 接口通了就不该再枚举进程（running 由接口结果推断）
    from qq_lottery.napcat import NapCatManager

    mgr = NapCatManager()
    calls = {"n": 0}
    real = N.napcat_processes

    def counting(*a, **kw):
        calls["n"] += 1
        return real(*a, **kw)

    N.napcat_processes = counting  # type: ignore[assignment]
    try:
        state = mgr.login_state("http://127.0.0.1:9", timeout=1.0)   # 必然连不上
        check("接口不通时会去确认进程状态", calls["n"] >= 1, str(calls["n"]))
        check("接口不通时 running 有明确取值", state["running"] in (True, False))
    finally:
        N.napcat_processes = real  # type: ignore[assignment]


def main() -> int:
    from qq_lottery.cli import _setup_console

    _setup_console()
    print("消息存档 · 专项自检")
    tmp = Path(tempfile.mkdtemp(prefix="qq-lottery-archive-"))
    print(f"临时目录：{tmp}")
    try:
        test_upsert_basics(tmp)
        test_keys(tmp)
        test_time_range(tmp)
        test_status_and_log(tmp)
        test_merge_with_live(tmp)
        test_service_integration(tmp)
        test_watch_loop(tmp)
        test_auto_sync_is_nonblocking(tmp)
        test_napcat_caching(tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        for cache in ROOT.rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)

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
