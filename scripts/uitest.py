#!/usr/bin/env python3
"""图形界面后端自检：本地服务的全部接口。

    python scripts/uitest.py

特点：
  · 在真实端口上启动服务（只监听回环地址）
  · 覆盖鉴权、防跨站、防路径穿越、导出下载等安全相关行为
  · 不依赖浏览器，可在无界面环境运行

全部通过时退出码为 0，任意一项失败退出码为 1。
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from qq_lottery import paths  # noqa: E402
from qq_lottery.config import Config  # noqa: E402
from qq_lottery.service import LotteryService  # noqa: E402
from qq_lottery.settings import ConfigFile  # noqa: E402
from qq_lottery.webapp import create_server  # noqa: E402

# 安全锁：本进程内禁止一切"向群里发消息"的调用。
# 事故教训：测试曾在用户真实群配置上运行，把测试开奖文案真的发进了群。
# 有了这道锁，即便测试误用了真实配置，发送也会被拒绝而不是发出去。
os.environ["QQLOTTERY_NO_SEND"] = "1"

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


# ---------------------------------------------------------------- HTTP 小工具


class Client:
    def __init__(self, port: int, token: str) -> None:
        self.base = f"http://127.0.0.1:{port}"
        self.token = token

    def request(
        self,
        path: str,
        method: str = "GET",
        body: dict | None = None,
        token: str | None = None,
        host: str | None = None,
        headers: dict[str, str] | None = None,
        raw_body: bytes | None = None,
    ) -> tuple[int, dict | bytes, dict]:
        url = f"{self.base}{path}"
        data = raw_body if raw_body is not None else (json.dumps(body or {}).encode("utf-8") if method == "POST" else None)
        req = urllib.request.Request(url, data=data, method=method)
        tok = self.token if token is None else token
        if tok:
            req.add_header("X-Lottery-Token", tok)
        if method == "POST":
            req.add_header("Content-Type", "application/json")
        if host:
            req.add_header("Host", host)
        for key, value in (headers or {}).items():
            req.add_header(key, value)
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                payload = resp.read()
                headers = dict(resp.headers)
                status = resp.status
        except urllib.error.HTTPError as exc:
            payload = exc.read()
            headers = dict(exc.headers)
            status = exc.code
        text = payload.decode("utf-8", errors="replace")
        if headers.get("Content-Type", "").startswith("application/json"):
            try:
                return status, json.loads(text), headers
            except json.JSONDecodeError:
                return status, {"_raw": text}, headers
        return status, text, headers

    def api(self, path: str, method: str = "GET", body: dict | None = None, **kwargs):
        status, payload, headers = self.request(f"/api/{path}", method, body, **kwargs)
        return status, payload


# ---------------------------------------------------------------- 测试
def test_service_direct(service: LotteryService) -> None:
    section("服务层（不经 HTTP）")
    res = service.get_config()
    check("get_config 返回配置", res.ok and res.data.get("days") == 3, str(res.message))
    check("配置里有程序目录与配置文件路径", bool(res.data.get("root")) and bool(res.data.get("config_path")))

    res = service.check_config()
    check("check_config 对离线记录返回就绪", res.ok and "就绪" in res.message, res.message)

    res = service.stats()
    check("stats 返回统计", res.ok, res.message)
    if res.ok:
        summary = res.data["summary"]
        check(f"候选人数与内核一致 ({summary['candidates']})", summary["candidates"] == 13, str(summary["candidates"]))
        check("排行包含中奖概率", all("chance_pct" in row for row in res.data["top"]))
        check("排行榜按发言条数降序", all(
            a["count"] >= b["count"] for a, b in zip(res.data["top"], res.data["top"][1:])
        ))

    res = service.draw({"winners": 2, "seed": 2024})
    check("draw 成功抽出 2 人", res.ok and len(res.data["winners"]) == 2, res.message)
    if res.ok:
        check("draw 返回开奖文案", "开奖" in res.data["group_message"])
        check("draw 记录随机种子", res.data["seed"] == 2024)
        check("draw 写入历史记录", res.data["history_saved_to"].endswith(".jsonl"))
        winners = res.data["winners"]
        check("中奖者不重复", len({w["user_id"] for w in winners}) == len(winners))
        check("中奖者有概率字段", all(0 < w["probability"] <= 1 for w in winners))

    res = service.history(limit=10)
    check("history 能读到刚才的记录", res.ok and res.data["total"] >= 1, str(res.data.get("total")))

    for kind, suffix in (("csv", ".csv"), ("json", ".json"), ("poster", ".html")):
        res = service.export(kind)
        check(f"export {kind} 生成文件", res.ok and res.data["file"].endswith(suffix), res.message)
        if res.ok:
            target = Path(res.data["path"])
            check(f"export {kind} 文件非空", target.is_file() and target.stat().st_size > 0)

    res = service.export("xml")
    check("export 拒绝未知类型", not res.ok and res.code == "bad_request")

    res = service.list_group_members()
    check("file 数据源下拒绝读取群成员", not res.ok and res.code == "not_onebot")

    # 参数校验
    res = service.draw({"winners": "abc"})
    check("draw 拒绝非法人数", not res.ok and res.code == "bad_request")
    res = service.draw({"days": "-1"})
    check("draw 拒绝非法天数", not res.ok and res.code in ("bad_request", "config_invalid"))

    snapshot = service.snapshot()
    check("snapshot 反映已读取状态", snapshot["has_messages"] is True and snapshot["last_draw"] is not None)


def test_http_basics(client: Client) -> None:
    section("HTTP 基础与安全")
    status, text, headers = client.request("/")
    check("首页返回 200", status == 200, f"status={status}")
    check("首页是 HTML", "text/html" in headers.get("Content-Type", ""))
    check("首页包含界面骨架", "QQ 群抽奖" in text and 'id="btn-draw"' in text)
    check("下发了 HttpOnly Cookie", "HttpOnly" in headers.get("Set-Cookie", ""))

    status, js, headers = client.request("/assets/app.js")
    check("脚本可访问", status == 200 and "javascript" in headers.get("Content-Type", ""))
    check("脚本里注入了会话令牌", client.token in js)
    check("脚本里没有留下占位符", "__LOTTERY_TOKEN__" not in js)
    check("脚本采用同源相对路径请求", 'fetch(`/api/' in js)
    status, css, headers = client.request("/assets/style.css")
    check("样式表可访问", status == 200 and "text/css" in headers.get("Content-Type", ""))

    status, payload, _ = client.request("/api/config", token="")
    check("缺少令牌时拒绝访问接口", status == 403 and payload.get("code") == "bad_token", str(status))
    status, payload, _ = client.request("/api/config", token="wrong-token")
    check("错误令牌被拒绝", status == 403 and payload.get("code") == "bad_token")

    # 防 DNS 重绑定：伪造 Host 头
    status, payload, _ = client.request("/api/config", host="evil.example.com")
    check("非回环 Host 被拒绝", status == 403 and payload.get("code") == "bad_host", str(status))

    status, payload, _ = client.request("/api/config", host="localhost:9999")
    check("localhost Host 允许访问", status == 200 and payload.get("ok") is True)

    # 防跨站：浏览器的 Origin / Sec-Fetch-Site 头
    status, payload, _ = client.request(
        "/api/config/save", "POST", {"days": 3}, headers={"Origin": "http://evil.example.com"}
    )
    check("跨站 Origin 被拒绝", status == 403 and payload.get("code") == "bad_origin", str(status))
    status, payload, _ = client.request(
        "/api/config/save", "POST", {"days": 3}, headers={"Sec-Fetch-Site": "cross-site"}
    )
    check("cross-site 请求被拒绝", status == 403 and payload.get("code") == "bad_origin", str(status))
    status, payload, _ = client.request(
        "/api/config", "GET", headers={"Sec-Fetch-Site": "same-origin"}
    )
    check("同源请求允许通过", status == 200 and payload.get("ok") is True)

    status, payload, _ = client.request("/api/nonexistent", token=client.token)
    check("未知接口返回 404 JSON", status == 404 and payload.get("ok") is False)

    status, payload, _ = client.request("/assets/../qq_lottery/cli.py", token=client.token)
    check("静态资源路径穿越被拦截", status in (403, 404), f"status={status}")

    status, payload, _ = client.request("/api/export/download?name=../../Windows/win.ini")
    check("下载路径穿越被拦截", status == 403 and payload.get("code") == "bad_path", str(status))

    status, payload, _ = client.request("/api/export/download?name=data/messages.json")
    check("允许下载 data 目录内的文件", status == 200, str(status))

    status, payload, _ = client.request(
        "/api/config/save", "POST", token=client.token, raw_body=b"{not json"
    )
    check("非法 JSON 请求体被拒绝", status == 400 and payload.get("code") == "bad_json", str(status))


def test_http_config(client: Client, service: LotteryService) -> None:
    section("HTTP 配置接口")
    status, payload = client.api("config")
    check("GET config 返回视图", status == 200 and payload["ok"] is True)
    cfg = payload["data"]
    check("视图包含必要字段", {"source", "days", "winners", "file", "onebot", "config_path"} <= set(cfg))
    check("视图标注记录文件是否存在", cfg["data_file_exists"] is True)

    status, payload = client.api("config/test", "POST", {})
    check("连接测试成功", status == 200 and payload["ok"] is True, str(payload.get("message")))

    # 保存：改几个值再读回
    status, payload = client.api("config/save", "POST", {
        "days": 5,
        "winners": 3,
        "weight_cap": 12,
        "blacklist": ["10001", "10002"],
        "exclude_roles": ["admin"],
        "weighting": False,
        "file": {"path": "data/sample_messages.csv", "format": "csv"},
    })
    check("保存配置成功", status == 200 and payload["ok"] is True, str(payload.get("message")))

    status, payload = client.api("config")
    saved = payload["data"]
    check("配置已持久化（days）", saved["days"] == 5)
    check("配置已持久化（winners）", saved["winners"] == 3)
    check("配置已持久化（黑名单）", saved["blacklist"] == ["10001", "10002"])
    check("配置已持久化（排除身份）", saved["exclude_roles"] == ["admin"])
    check("配置已持久化（文件路径）", saved["file"]["path"] == "data/sample_messages.csv")

    # 磁盘上那份（隔离的）配置也应该是新值，说明界面改动确实写进了配置文件
    on_disk = json.loads(service.config_file.path.read_text(encoding="utf-8"))
    check("配置文件落盘正确", on_disk["days"] == 5 and on_disk["blacklist"] == ["10001", "10002"])
    check("配置文件不含界面临时键", "data_file_exists" not in on_disk)

    # 新配置生效：CSV 文件 + 5 天窗口 + 黑名单 + 排除管理员
    status, payload = client.api("stats", "POST", {"reload": True})
    check("换配置后重新统计成功", status == 200 and payload["ok"] is True, str(payload.get("message")))
    if payload["ok"]:
        summary = payload["data"]["summary"]
        ids = {row["user_id"] for row in payload["data"]["top"]}
        check("黑名单在接口层生效", "10001" not in ids and "10002" not in ids)
        check("管理员被排除", "10004" not in ids, str(sorted(ids))[:120])
        check("窗口变为 5 天", summary["days"] == 5)
        check("从 CSV 文件读到了消息", summary["messages_total"] > 300, str(summary["messages_total"]))

    # 非法配置要被拒绝
    status, payload = client.api("config/save", "POST", {"days": -3})
    check("非法天数被拒绝", status == 400 and payload["ok"] is False, str(status))
    check("拒绝时返回可读原因", bool(payload.get("message")))

    # 还原成默认演示配置
    status, payload = client.api("config/save", "POST", {
        "days": 3,
        "winners": 1,
        "weight_cap": 0,
        "blacklist": [],
        "exclude_roles": [],
        "weighting": True,
        "file": {"path": "data/messages.json", "format": "auto"},
    })
    check("配置已还原", status == 200 and payload["ok"] is True)


def test_http_draw_and_export(client: Client) -> None:
    section("HTTP 开奖与导出")
    client.api("stats", "POST", {"reload": True})

    status, payload = client.api("draw", "POST", {"winners": 3, "seed": 777})
    check("开奖接口返回成功", status == 200 and payload["ok"] is True, str(payload.get("message")))
    data = payload["data"]
    check("抽中 3 人", len(data["winners"]) == 3, str(len(data["winners"])))
    check("返回概览数据", {"candidates", "window", "days_label"} <= set(data["summary"]))
    check("返回开奖文案", len(data["group_message"]) > 20)
    check("返回 id 与时间", bool(data["id"]) and bool(data["drawn_at"]))
    check("历史文件已写入", bool(data["history_saved_to"]))

    status, payload = client.api("history?limit=5")
    check("历史接口可用", status == 200 and payload["ok"] is True)
    check("历史里能看到本次记录", payload["data"]["items"][0]["seed"] == 777, str(payload["data"]["items"][0]["seed"]))

    for kind, suffix in (("csv", ".csv"), ("json", ".json"), ("poster", ".html")):
        status, payload = client.api("export", "POST", {"kind": kind})
        check(f"导出 {kind} 成功", status == 200 and payload["ok"] is True, str(payload.get("message")))
        if payload["ok"]:
            name = payload["data"]["file"]
            check(f"导出 {kind} 文件名正确", name.endswith(suffix), name)
            status, body, headers = client.request(
                f"/api/export/download?name={urllib.parse.quote(payload['data']['path'])}"
            )
            check(f"下载 {kind} 成功", status == 200 and len(body) > 0, str(status))

    status, payload = client.api("post", "POST", {})
    check("离线数据源下发群被拒绝", status == 400 and payload["code"] == "not_onebot", str(payload.get("message")))

    status, payload = client.api("export", "POST", {"kind": "zip"})
    check("未知导出类型被拒绝", status == 400 and payload["ok"] is False)

    status, payload = client.api("draw", "POST", {"winners": 1, "days": 0.0001})
    check("无人符合条件时给出可读提示", status == 400 and payload["code"] == "no_candidates", str(payload.get("message")))


def test_http_onebot_mock(client: Client) -> None:
    section("HTTP · 模拟机器人数据源")
    from http.server import BaseHTTPRequestHandler, HTTPServer

    now = time.time()

    class Mock(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def _reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            action = self.path.strip("/").split("/")[-1]
            length = int(self.headers.get("Content-Length") or 0)
            params = json.loads(self.rfile.read(length) or b"{}")
            if action == "get_login_info":
                return self._reply({"status": "ok", "data": {"user_id": 999, "nickname": "测试机器人"}})
            if action == "get_group_member_list":
                return self._reply({"status": "ok", "data": [
                    {"user_id": 50001, "nickname": "群主大人", "role": "owner"},
                    {"user_id": 50002, "nickname": "管理员小张", "role": "admin"},
                    {"user_id": 50003, "nickname": "普通群友", "role": "member"},
                ]})
            if action == "get_group_msg_history":
                seq = params.get("message_seq")
                newest = 300 if seq is None else int(seq) - 1
                oldest = max(1, newest - 99)
                rows = []
                for i in range(newest, oldest - 1, -1):
                    uid = 50000 + (i % 5)
                    rows.append({
                        "group_id": 123456789,
                        "message_id": 700000 + i,
                        "message_seq": i,
                        "user_id": uid,
                        "time": int(now - (300 - i) * 900),
                        "sender": {"user_id": uid, "nickname": f"群友{i % 5}", "role": "member"},
                    })
                return self._reply({"status": "ok", "data": {"messages": rows}})
            if action == "send_group_msg":
                Mock.sent.append(params)
                return self._reply({"status": "ok", "data": {"message_id": 1}})
            return self._reply({"status": "failed", "retcode": 1404, "message": "unknown"})

    Mock.sent = []
    server = HTTPServer(("127.0.0.1", 0), Mock)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    port = server.server_address[1]

    try:
        status, payload = client.api("config/save", "POST", {
            "source": "onebot",
            "group_id": "123456789",
            "archive": {"enabled": False, "path": "_unused.db", "auto_sync": False},
            "onebot": {"base_url": f"http://127.0.0.1:{port}", "access_token": "", "page_size": 100, "max_pages": 10},
        })
        check("切换到机器人数据源", status == 200 and payload["ok"] is True, str(payload.get("message")))

        status, payload = client.api("config/test", "POST", {})
        check("机器人连接测试通过", status == 200 and payload["ok"] is True, str(payload.get("message")))
        if payload["ok"]:
            check("连接测试返回机器人昵称", payload["data"]["nickname"] == "测试机器人")

        status, payload = client.api("members")
        check("读取群成员成功", status == 200 and payload["ok"] is True, str(payload.get("message")))
        if payload["ok"]:
            check("成员列表含身份信息", payload["data"][0]["role"] == "owner" and len(payload["data"]) == 3)

        status, payload = client.api("stats", "POST", {"reload": True})
        check("机器人数据源统计成功", status == 200 and payload["ok"] is True, str(payload.get("message")))
        if payload["ok"]:
            check("机器人自身被排除", all(r["user_id"] != "999" for r in payload["data"]["top"]))
            check("统计到 5 位群友", payload["data"]["summary"]["candidates"] == 5, str(payload["data"]["summary"]["candidates"]))

        status, payload = client.api("draw", "POST", {"winners": 2, "seed": 11})
        check("机器人数据源开奖成功", status == 200 and payload["ok"] is True, str(payload.get("message")))

        # 先确认安全锁拦得住（这才是测试环境该有的行为）
        status, payload = client.api("post", "POST", {})
        check(
            "测试环境下发送被安全锁拒绝",
            status == 400 and payload.get("code") == "send_blocked",
            str(payload.get("message"))[:80],
        )
        check("被拒绝时没有真的发出消息", len(Mock.sent) == 0, str(len(Mock.sent)))

        # 再显式临时解锁，验证 HTTP 发送链路本身是通的。
        # 注意：目标只是本进程内的模拟服务器（127.0.0.1 随机端口），
        # 从不指向真实 NapCat，所以这里不会打扰任何真实群。
        os.environ.pop("QQLOTTERY_NO_SEND", None)
        try:
            status, payload = client.api("post", "POST", {})
            check("临时解锁后发送链路可用", status == 200 and payload["ok"] is True, str(payload.get("message")))
            check("模拟服务器确实收到群消息", len(Mock.sent) == 1, str(len(Mock.sent)))
            if Mock.sent:
                check("群消息用配置里的群号", Mock.sent[0]["group_id"] == 123456789)
                check("群消息内容是开奖文案", "开奖" in Mock.sent[0]["message"])
        finally:
            os.environ["QQLOTTERY_NO_SEND"] = "1"
        check("安全锁已恢复", os.environ.get("QQLOTTERY_NO_SEND") == "1")
    finally:
        server.shutdown()
        server.server_close()
        # 还原配置，避免影响后续使用
        client.api("config/save", "POST", {
            "source": "file",
            "group_id": "",
            "file": {"path": "data/messages.json", "format": "auto"},
            "archive": {"enabled": False, "path": "_unused.db", "auto_sync": False},
            "onebot": {"base_url": "http://127.0.0.1:3000", "access_token": ""},
        })


def test_path_independence() -> None:
    section("路径基准")
    from qq_lottery import paths as p

    check("project_root 指向程序目录", (p.project_root() / "run_lottery.py").is_file())
    check("相对路径基于程序目录解析", p.resolve("data/messages.json") == p.project_root() / "data" / "messages.json")
    check("绝对路径原样返回", str(p.resolve("C:/Windows")) .lower().startswith("c:"))
    cfg = Config(source="file", file_path="data/messages.json", history_file="data/history.jsonl")
    check("Config 提供绝对路径属性", cfg.file_path_abs.is_file())
    check("历史文件路径可解析", cfg.history_file_abs is not None and cfg.history_file_abs.name == "history.jsonl")


def test_frontend_sanity() -> None:
    """静态检查前端资源：HTML 里不能有重复 id，JS 里引用的 id 必须存在。"""
    section("前端资源静态检查")
    import re

    html = (ROOT / "webapp" / "index.html").read_text(encoding="utf-8")
    js = (ROOT / "webapp" / "assets" / "app.js").read_text(encoding="utf-8")
    css = (ROOT / "webapp" / "assets" / "style.css").read_text(encoding="utf-8")
    webapp_src = (ROOT / "qq_lottery" / "webapp.py").read_text(encoding="utf-8")

    ids = re.findall(r'\bid="([^"]+)"', html)
    duplicates = sorted({name for name in ids if ids.count(name) > 1})
    check("HTML 中没有重复的 id", not duplicates, f"重复：{duplicates}")
    # 图标护栏：favicon 决定 Edge 应用窗口在任务栏里的图标，缺了会退回 Edge 默认图标
    for icon_file in ('icon.ico', 'icon-32.png', 'icon-180.png'):
        path = ROOT / 'assets' / icon_file
        check(f'图标资源存在：{icon_file}', path.is_file() and path.stat().st_size > 0)
    check('页面引用了 favicon', 'rel="icon"' in html and '/icon.ico' in html)
    check('favicon 路由已注册', '/icon.ico' in webapp_src and 'ICON_DIR' in webapp_src)
    check('favicon 路由不要求令牌（浏览器不会带）', 'ICON_FILES.get(path)' in webapp_src)

    check("HTML 里保留了令牌占位符或已注入机制", "__LOTTERY_TOKEN__" in js)

    used = set(re.findall(r"\$\('#([A-Za-z0-9_-]+)'\)", js))
    missing = sorted(name for name in used if name not in set(ids))
    check("JS 里 $(#id) 引用的元素都存在", not missing, f"缺失：{missing}")

    # 关键：同一个 els 属性不能被赋两次（曾经因为 winners 被覆盖导致中奖名单不渲染）
    cache_block = js.split("function cacheEls()", 1)[1].split("}\n", 1)[0]
    props = re.findall(r"^\s*([A-Za-z0-9_]+):\s*", cache_block, re.M)
    repeated = sorted({name for name in props if props.count(name) > 1})
    check("els 缓存键没有重复赋值", not repeated, f"重复：{repeated}")

    check("样式里限制了 hidden 属性", "[hidden] { display: none !important; }" in css)
    check("样式引用了设计变量", "--brand" in css and "--accent" in css)
    check("JS 不含调试残留", "console.log(" not in js and "debugger" not in js)


def test_nt_db_converter() -> None:
    """用合成的 QQ NT 明文库验证 nt_msg.db 转换器（含 protobuf 正文解析）。"""
    section("QQ NT 明文库转换器")
    import importlib.util
    import sqlite3
    import tempfile

    spec = importlib.util.spec_from_file_location("convert_nt_db", ROOT / "scripts" / "convert_nt_db.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)

    def varint(n: int) -> bytes:
        out = bytearray()
        while True:
            byte = n & 0x7F
            n >>= 7
            out.append(byte | (0x80 if n else 0))
            if not n:
                return bytes(out)

    def field(num: int, payload: bytes) -> bytes:
        return varint((num << 3) | 2) + varint(len(payload)) + payload

    def text_body(text: str) -> bytes:
        """模拟 QQ NT 的正文结构：40800 -> 47602 -> 47603(文本)"""
        return field(47602, field(47603, text.encode("utf-8")))

    # wire 解析的基础校验
    check("protobuf 解析能取出嵌套文本", mod.extract_content(text_body("晚上好")) == "晚上好")
    check("protobuf 解析能处理长文本", mod.extract_content(text_body("测试" * 40)) == "测试" * 40)
    check("空正文返回空串", mod.extract_content(b"") == "" and mod.extract_content(None) == "")
    check("解析多字段消息时取最长文本", mod.extract_content(field(1, b"x") + text_body("这是一条正常长度的消息")) == "这是一条正常长度的消息")

    now = int(time.time())
    with tempfile.TemporaryDirectory() as tmp:
        tmp_path = Path(tmp)
        db = tmp_path / "nt_msg.db"
        con = sqlite3.connect(db)
        con.execute(
            'CREATE TABLE group_msg_table ('
            '"40001" INTEGER, "40020" INTEGER, "40021" INTEGER, "40027" INTEGER, '
            '"40050" INTEGER, "40800" BLOB)'
        )
        rows = [
            (1, 10001, 555001, 555001, (now - 3600) * 1000, text_body("大家晚上好")),
            (2, 10002, 555001, 555001, (now - 7200) * 1000, text_body("+1")),
            (3, 10001, 555001, 555001, (now - 10800) * 1000, text_body("这题我会")),
            (4, 10003, 555002, 555002, (now - 1800) * 1000, text_body("另一个群的消息")),
            (5, 10004, 555001, 555001, (now - 900) * 1000, b"\x00\x01\x02\x03"),  # 无文本
            (6, 10005, 555001, 555001, (now - 10 * 86400) * 1000, text_body("十天前的老消息")),
        ]
        con.executemany('INSERT INTO group_msg_table VALUES (?,?,?,?,?,?)', rows)
        con.commit()
        con.close()

        ok, reason = mod.check_plaintext(db)
        check("识别出已解密的库", ok and "已解密" in reason)

        out = tmp_path / "messages.json"
        jsonl = tmp_path / "messages.jsonl"
        code = mod.export(tmp_path, "555001", None, None, out, jsonl)
        check("转换器执行成功", code == 0, f"退出码 {code}")
        if code == 0:
            records = json.loads(out.read_text(encoding="utf-8"))
            check("只导出目标群的消息", {r["group_id"] for r in records} == {"555001"})
            check("无文本消息被跳过", len(records) == 4, f"实际 {len(records)}")
            check("文本内容解析正确", any(r["content"] == "大家晚上好" for r in records))
            check("QQ 号正确", {r["user_id"] for r in records} == {"10001", "10002", "10005"})
            check("时间戳被换算成秒", all(r["time"] < 1e11 for r in records))
            check("结果按时间升序", all(a["time"] <= b["time"] for a, b in zip(records, records[1:])))
            check("JSONL 同步产出", jsonl.is_file() and len(jsonl.read_text(encoding="utf-8").strip().splitlines()) == 4)

            # 导出的文件必须能被抽奖工具直接解析
            from qq_lottery.sources import load_from_file

            parsed = load_from_file(out)
            check("抽奖工具能直接读取导出结果", len(parsed) == 4, f"实际 {len(parsed)}")

            # 天数过滤
            out2 = tmp_path / "recent.json"
            mod.export(tmp_path, "555001", 1.0, None, out2, None)
            recent = json.loads(out2.read_text(encoding="utf-8"))
            check("--days 过滤掉过期消息", len(recent) == 3, f"实际 {len(recent)}")

        # 加密库要被明确拒绝并给出可读原因
        enc = tmp_path / "encrypted.db"
        enc.write_bytes(b"SQLite header 3\x00" + b"\x00" * 64)
        ok2, reason2 = mod.check_plaintext(enc)
        check("加密库被识别为未解密", not ok2 and "加密" in reason2, reason2)


def test_doc_guards() -> None:
    """文档与脚本的护栏：Markdown 相对链接不能是死链，自写的 .ps1 必须是纯 ASCII。"""
    section("文档与脚本护栏")
    import re

    md_files = sorted([ROOT / "README.md"] + list((ROOT / "docs").glob("*.md")) + list((ROOT / "maintenance").glob("*.md")))
    check("至少存在一份 Markdown 文档", len(md_files) >= 3, f"实际 {len(md_files)}")

    broken: list[str] = []
    total_links = 0
    for md in md_files:
        text = md.read_text(encoding="utf-8")
        # 去掉代码块，避免把示例路径当成真链接
        text = re.sub(r"```.*?```", "", text, flags=re.S)
        for target in re.findall(r"!?\[[^\]]*\]\(([^)]+)\)", text):
            target = target.strip().split("#")[0]
            if not target or "://" in target or target.startswith("mailto:"):
                continue
            total_links += 1
            resolved = (md.parent / target).resolve()
            if not resolved.exists():
                broken.append(f"{md.name} -> {target}")
    check(f"Markdown 相对链接都有效（共检查 {total_links} 个）", not broken, "；".join(broken[:6]))

    # 中文 Windows 上 PowerShell 5.1 用 GBK 解析 .ps1，非 ASCII 字面量会被截断出错
    for name in ("napcat_check.ps1", "抽奖.ps1"):
        p = (ROOT / "scripts" / name) if name.startswith("napcat") else (ROOT / name)
        if not p.is_file():
            check(f"{name} 存在", False)
            continue
        raw = p.read_bytes()
        bad = [i for i, b in enumerate(raw) if b > 127]
        check(
            f"{name} 是纯 ASCII（避开 PowerShell 5.1 的 GBK 解析问题）",
            not bad,
            f"发现 {len(bad)} 个非 ASCII 字节",
        )

    for bat in ("lottery.bat", "启动界面.bat", "scripts/archive-watch.bat"):
        p = ROOT / bat
        if not p.is_file():
            continue
        raw = p.read_bytes()
        bad = [i for i, b in enumerate(raw) if b > 127]
        check(f"{bat} 是纯 ASCII", not bad, f"发现 {len(bad)} 个非 ASCII 字节")

    guide = ROOT / "docs" / "NapCat配置指南.md"
    check("NapCat 指南存在", guide.is_file())
    if guide.is_file():
        text = guide.read_text(encoding="utf-8")
        check("NapCat 指南覆盖了下载链接", "NapCat.Shell.Windows.OneKey.zip" in text)
        check("NapCat 指南说明了启用 HTTP 服务端", "保存时启用" in text and "httpServers" in text)
        check("NapCat 指南提到用诊断脚本验证", "napcat_check.ps1" in text)
        check(
            "NapCat 指南说明了执行策略问题",
            "ExecutionPolicy" in text and "禁止运行脚本" in text,
        )
        check("NapCat 指南说明了账号风险", "风控" in text or "风险" in text)
        check("NapCat 指南说明了开机自启", "napcat-autostart" in text and "Startup" in text)
        check("NapCat 指南说明了历史深度限制", "缓存深度" in text or "历史消息深度" in text)


def test_live_onebot_if_configured() -> None:
    """如果用户已经配好了真实机器人，就顺带验证一次真实链路（可选，失败不算错）。"""
    section("真实机器人连通性（若已配置）")
    real_config = paths.project_root() / "config.json"
    if not real_config.is_file():
        check("跳过：没有 config.json", True)
        return
    try:
        data = json.loads(real_config.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        check("跳过：config.json 无法解析", True)
        return
    if data.get("source") != "onebot" or not str(data.get("group_id") or "").strip():
        check("跳过：当前配置未使用机器人数据源", True)
        return

    # 默认不打扰真实机器人：只有显式设置 QQLOTTERY_LIVE_CHECK=1 才做这次检查
    if os.environ.get("QQLOTTERY_LIVE_CHECK", "").strip() != "1":
        check("跳过真实机器人检查（需要时设 QQLOTTERY_LIVE_CHECK=1 开启）", True)
        return

    import urllib.request

    base = str((data.get("onebot") or {}).get("base_url") or "http://127.0.0.1:3000").rstrip("/")
    token = str((data.get("onebot") or {}).get("access_token") or "")
    group = str(data["group_id"])
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"

    def call(action: str, payload: dict) -> dict:
        req = urllib.request.Request(
            f"{base}/{action}",
            data=json.dumps(payload).encode("utf-8"),
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=20) as resp:
            return json.loads(resp.read().decode("utf-8"))

    try:
        login = call("get_login_info", {})
    except Exception as exc:
        # 机器没开不算测试失败，只是服务当前不可用
        check(f"机器人未运行（跳过真实链路验证）", True)
        print(f"  [SKIP] {exc}")
        return

    bot = (login.get("data") or {})
    check("机器人接口可用", login.get("status") == "ok", str(login.get("message")))
    print(f"  [INFO] 机器人：{bot.get('nickname')} ({bot.get('user_id')})")

    try:
        hist = call("get_group_msg_history", {"group_id": int(group), "count": 100})
    except Exception as exc:
        check("群消息历史接口可用", False, str(exc))
        return
    msgs = ((hist.get("data") or {}).get("messages")) or []
    check(f"能拉到群 {group} 的历史消息（{len(msgs)} 条）", len(msgs) > 0, "返回 0 条")
    if msgs:
        times = sorted(m["time"] for m in msgs)
        span_min = (times[-1] - times[0]) / 60
        print(f"  [INFO] 历史深度：{len(msgs)} 条，跨度 {span_min:.1f} 分钟")
        if span_min < 60 * 24:
            print("  [INFO] 提示：缓存较浅，抽奖窗口内的实际数据等于 NapCat 已在线时长")


def test_send_guards() -> None:
    """发送安全锁：测试进程内绝不允许把消息发进真实群。"""
    section("发送安全锁（防止测试消息进群）")
    from qq_lottery.service import _is_local_endpoint
    from qq_lottery.sources import SEND_BLOCK_ENV, OneBotClient, SendBlockedError, sending_blocked

    check(f"测试进程已设置 {SEND_BLOCK_ENV}", os.environ.get(SEND_BLOCK_ENV) == "1")
    check("发送开关处于禁用状态", sending_blocked())

    blocked = False
    try:
        OneBotClient("http://127.0.0.1:3000").send_group_msg("123456789", "这条不该被发出去")
    except SendBlockedError:
        blocked = True
    except Exception as exc:  # 网络错误也算没发出去，但不算通过
        blocked = f"意外的异常类型：{type(exc).__name__}"
    check("向群里发消息被硬锁拒绝", blocked is True, str(blocked))

    for url, expected in (
        ("http://127.0.0.1:3000", True),
        ("http://localhost:3000", True),
        ("http://192.168.1.5:3000", True),
        ("http://10.1.2.3:3000", True),
        ("http://8.8.8.8:3000", False),
        ("http://evil.example.com:3000", False),
    ):
        got = _is_local_endpoint(url)
        check(f"地址校验 {'放行' if expected else '拒绝'} {url}", got is expected, f"实际 {got}")


def test_bot_manager_and_run_all() -> None:
    """机器人管理面板：状态、群列表、扫码、一键开跑。

    全部对着本进程内起的**模拟 NapCat** 跑，绝不触碰用户真实的机器人服务。
    """
    section("机器人管理 · 一键开跑")
    import tempfile
    from http.server import BaseHTTPRequestHandler, HTTPServer

    from qq_lottery import napcat as napcat_mod

    now = int(time.time())

    class MockNapCat(BaseHTTPRequestHandler):
        logged_in = True
        sent: list[dict] = []

        def log_message(self, *args):
            pass

        def _reply(self, payload):
            body = json.dumps(payload).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_POST(self):  # noqa: N802
            action = self.path.strip("/").split("/")[-1]
            length = int(self.headers.get("Content-Length") or 0)
            params = json.loads(self.rfile.read(length) or b"{}")

            if action == "get_login_info":
                if not MockNapCat.logged_in:
                    return self._reply({"status": "failed", "retcode": 1, "message": "not logged in"})
                return self._reply({"status": "ok", "retcode": 0, "data": {"user_id": 10086, "nickname": "测试机器人"}})
            if action == "get_group_list":
                return self._reply({"status": "ok", "retcode": 0, "data": [
                    {"group_id": 123456789, "group_name": "大群", "member_count": 441},
                    {"group_id": 987654321, "group_name": "小群", "member_count": 11},
                ]})
            if action == "get_group_msg_history":
                rows = []
                for i in range(120):
                    uid = 60000 + (i % 6)
                    rows.append({
                        "group_id": 123456789,
                        "message_id": 900000 + i,
                        "message_seq": 900000 + i,
                        "user_id": uid,
                        "time": now - (120 - i) * 600,
                        "sender": {"user_id": uid, "nickname": f"群友{uid % 100}", "role": "member"},
                    })
                return self._reply({"status": "ok", "retcode": 0, "data": {"messages": list(reversed(rows))}})
            if action == "get_group_member_list":
                # 昵称刻意避开"机器人/bot/助手"等关键字，否则会被机器人识别规则误杀
                return self._reply({"status": "ok", "retcode": 0, "data": [
                    {"user_id": 60001, "nickname": "甲", "role": "owner"},
                    {"user_id": 60002, "nickname": "乙", "role": "admin"},
                    {"user_id": 60003, "nickname": "丙", "role": "member"},
                    {"user_id": 60004, "nickname": "丁", "role": "member"},
                    {"user_id": 60005, "nickname": "戊", "role": "member"},
                ]})
            if action == "send_group_msg":
                MockNapCat.sent.append(params)
                return self._reply({"status": "ok", "retcode": 0, "data": {"message_id": 1}})
            return self._reply({"status": "failed", "retcode": 1404, "message": "unknown"})

    MockNapCat.sent = []
    mock = HTTPServer(("127.0.0.1", 0), MockNapCat)
    threading.Thread(target=mock.serve_forever, daemon=True).start()
    mock_base = f"http://127.0.0.1:{mock.server_address[1]}"

    tmp_dir = Path(tempfile.mkdtemp(prefix="qq-lottery-botman-"))
    try:
        cfg_path = tmp_dir / "config.json"
        cfg_path.write_text(
            json.dumps(
                {
                    "source": "onebot",
                    "group_id": "",
                    "days": 3,
                    "winners": 2,
                    "weight_cap": 0,
                    "history_file": str(tmp_dir / "history.jsonl"),
                    "archive": {"enabled": True, "path": str(tmp_dir / "archive.db"), "auto_sync": False},
                # 存档指向临时目录，绝不碰真实的 data/archive.db
                "archive": {"enabled": True, "path": str(tmp_dir / "archive.db"), "auto_sync": False},
                    "file": {"path": "data/messages.json", "format": "auto"},
                    "onebot": {"base_url": mock_base, "access_token": ""},
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        mock_service = LotteryService(ConfigFile(cfg_path))
        srv, tok = create_server(mock_service, port=0)
        threading.Thread(target=srv.serve_forever, daemon=True).start()
        cli = Client(srv.server_address[1], tok)
        try:
            # 状态接口
            status_code, payload = cli.api("bot/state")
            check("bot/state 可用", status_code == 200 and payload["ok"] is True, str(payload.get("message")))
            data = payload["data"]
            check("状态里报告已登录", data.get("logged_in") is True)
            check("状态里带机器人身份", (data.get("bot") or {}).get("nickname") == "测试机器人")
            check("状态里说明发送是否被禁", "send_disabled" in data)

            status_code, payload = cli.api("bot/info")
            check("bot/info 可用", status_code == 200 and payload["ok"] is True)

            # 群列表
            status_code, payload = cli.api("bot/groups", "POST", {})
            check("bot/groups 可用", status_code == 200 and payload["ok"] is True, str(payload.get("message")))
            groups = payload["data"] if payload["ok"] else []
            check("群列表按人数降序", [g["group_id"] for g in groups] == ["123456789", "987654321"], str(groups))
            check("群列表带成员数", groups and groups[0]["member_count"] == 441)

            # 一键开跑：不填群号，应自动选人最多的群
            status_code, payload = cli.api("bot/run-all", "POST", {"winners": 3, "days": 3})
            check("一键开跑成功", status_code == 200 and payload["ok"] is True, str(payload.get("message"))[:90])
            if payload["ok"]:
                out = payload["data"]
                check("自动选中人最多的群", out.get("group_id") == "123456789", str(out.get("group_id")))
                check("抽到 3 人", len(out.get("winners", [])) == 3, str(len(out.get("winners", []))))
                check("返回执行步骤", isinstance(out.get("steps"), list) and len(out["steps"]) >= 4)
                check("步骤全部成功", all(s["ok"] for s in out["steps"]), str(out["steps"]))
                check("明确标记未发群", out.get("posted") is False)
                check("标记发送是否被禁", "send_disabled" in out)
                check("设置已写回配置", json.loads(cfg_path.read_text(encoding="utf-8"))["group_id"] == "123456789")

            # 关键安全断言：整个一键开跑过程绝不允许发出任何群消息
            check("一键开跑没有发出任何群消息", len(MockNapCat.sent) == 0, f"实际发送 {len(MockNapCat.sent)} 次")

            # ── 全群成员抽取：候选来自群成员列表，每人等概率 ──
            status_code, payload = cli.api("draw-all-members", "POST", {"winners": 3, "group_id": "123456789"})
            check("全群抽取接口可用", status_code == 200 and payload["ok"] is True, str(payload.get("message"))[:80])
            if payload["ok"]:
                allm = payload["data"]
                check("全群模式标记正确", allm.get("pool") == "all_members")
                check("候选来自群成员列表（5 人）", allm.get("candidate_count") == 5, str(allm.get("candidate_count")))
                check("全群模式抽到 3 人", len(allm.get("winners", [])) == 3, str(len(allm.get("winners", []))))
                # probability 是"当轮条件概率"：池子逐轮缩小，所以是 1/5、1/4、1/3…
                # 全群等概率的正确含义是：第一轮每人都是 1/N，且每轮都是 1/剩余人数
                check(
                    "第一轮每人概率都是 1/N（真正的等概率）",
                    abs(allm["winners"][0]["probability"] - 1 / 5) < 1e-5,
                    str(allm["winners"][0]["probability"]),
                )
                check(
                    "每轮概率等于 1/剩余人数",
                    all(
                        abs(w["probability"] - 1 / (5 - i)) < 1e-5
                        for i, w in enumerate(allm["winners"])
                    ),
                    str([w["probability"] for w in allm["winners"]]),
                )
                check(
                    "全群模式的中奖者发言条数为 0（与活跃度无关）",
                    all(w["count"] == 0 for w in allm["winners"]),
                    str([w["count"] for w in allm["winners"]]),
                )
                check("全群模式公平性说明含等概率", "等概率" in allm["fairness"]["method_label"])
                check("全群模式不涉及时间窗口覆盖", "data_coverage" not in allm["fairness"])
                check("全群模式返回开奖文案", "开奖" in allm.get("group_message", ""))
            check("全群抽取没有发出任何群消息", len(MockNapCat.sent) == 0, f"实际 {len(MockNapCat.sent)} 次")

            # 一键开跑切到全群模式：应跳过消息历史
            status_code, payload = cli.api("bot/run-all", "POST", {"winners": 2, "pool": "all_members"})
            check("一键开跑支持全群模式", status_code == 200 and payload["ok"] is True, str(payload.get("message"))[:80])
            if payload["ok"]:
                run_all_data = payload["data"]
                check("一键开跑全群模式返回 pool", run_all_data.get("pool") == "all_members")
                check("一键开跑全群模式候选为全群", run_all_data.get("candidate_count") == 5)
                check(
                    "一键开跑全群模式第一轮为 1/N",
                    abs(run_all_data["winners"][0]["probability"] - 1 / 5) < 1e-5,
                    str(run_all_data["winners"][0]["probability"]),
                )
                check(
                    "一键开跑全群模式跳过消息历史",
                    any("无需读取消息历史" in s["message"] for s in run_all_data["steps"]),
                )
            check("一键开跑全群模式也没发出消息", len(MockNapCat.sent) == 0, str(len(MockNapCat.sent)))

            # 未登录时应给出可读状态且不抛异常
            MockNapCat.logged_in = False
            status_code, payload = cli.api("bot/state")
            check("未登录状态可读", status_code == 200 and payload["ok"] is True)
            print('    [DEBUG] bot/state =', json.dumps(payload["data"], ensure_ascii=False)[:200])
            check("未登录时 logged_in 为假", payload["data"].get("logged_in") is False, repr(payload["data"].get("logged_in")))
            status_code, payload = cli.api("bot/run-all", "POST", {"winners": 1})
            check("未登录时一键开跑失败并给出原因", payload["ok"] is False and payload.get("code") == "onebot_unreachable", str(payload.get("message"))[:80])
            check("未登录时也没有发出消息", len(MockNapCat.sent) == 0, str(len(MockNapCat.sent)))
            MockNapCat.logged_in = True

            # 安全锁下，即便用户点「发送到群」也必须被拒绝
            status_code, payload = cli.api("post", "POST", {})
            check("安全锁下发送到群被拒绝", payload["ok"] is False and payload.get("code") in ("send_blocked", "no_draw"), str(payload.get("message"))[:60])
            check("被拒后依然没有任何消息发出", len(MockNapCat.sent) == 0, str(len(MockNapCat.sent)))
        finally:
            srv.shutdown()
            srv.server_close()
    finally:
        mock.shutdown()
        mock.server_close()
        shutil.rmtree(tmp_dir, ignore_errors=True)

    # NapCat 目录识别：不能把一键安装包里的 bootmain 当成安装根
    inst = napcat_mod.find_install()
    if inst:
        check("识别到的是 Shell 安装根目录", inst.shell.name.endswith(".Shell"), inst.shell.name)
        check("Shell 目录里确实有 QQ.exe", (inst.shell / "QQ.exe").is_file())
        check("能定位到 config 目录", inst.config_dir is not None and inst.config_dir.is_dir(), str(inst.config_dir))
    else:
        check("本机已安装 NapCat（未安装时跳过）", True)

    check("端口探测对未监听端口返回假", napcat_mod.port_listening(1) is False)


def main() -> int:
    from qq_lottery.cli import _setup_console

    _setup_console()
    print("QQ 群抽奖 · 图形界面后端自检")

    # 关键：在隔离的临时配置上测试，绝不碰用户真实的 config.json。
    # 以前的写法是「备份用户配置 → 就地修改 → 还原」，一旦中途异常就会破坏真实配置。
    tmp_dir = Path(tempfile.mkdtemp(prefix="qq-lottery-uitest-"))
    test_config = tmp_dir / "config.json"
    test_config.write_text(
        json.dumps(
            {
                "source": "file",
                "group_id": "123456789",
                "days": 3,
                "winners": 1,
                "weighting": True,
                "weight_cap": 0,
                "blacklist": [],
                "exclude_roles": [],
                "history_file": str(tmp_dir / "history.jsonl"),
                "file": {"path": "data/messages.json", "format": "auto"},
                "archive": {"enabled": False, "path": "_unused.db", "auto_sync": False},
                "onebot": {"base_url": "http://127.0.0.1:1", "access_token": ""},
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    service = LotteryService(ConfigFile(test_config))
    server, token = create_server(service, port=0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = Client(server.server_address[1], token)
    print(f"服务已启动：{client.base}")
    print(f"隔离配置：{test_config}")

    try:
        test_path_independence()
        test_frontend_sanity()
        test_doc_guards()
        test_send_guards()
        test_nt_db_converter()
        test_bot_manager_and_run_all()
        test_service_direct(service)
        test_http_basics(client)
        test_http_config(client, service)
        test_http_draw_and_export(client)
        test_http_onebot_mock(client)
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(tmp_dir, ignore_errors=True)
        # 清理本次自检可能写进工作区的产物
        removed = 0
        for pattern in ("test_*.json", "test_*.csv", "test_*.jsonl", "x.json"):
            for item in (paths.project_root() / "data").glob(pattern):
                if item.is_file():
                    item.unlink()
                    removed += 1
        for folder in ("out",):
            target = paths.project_root() / folder
            if target.is_dir():
                for item in target.glob("*"):
                    if item.is_file():
                        item.unlink()
                        removed += 1
        for cache in paths.project_root().rglob("__pycache__"):
            shutil.rmtree(cache, ignore_errors=True)
        if removed:
            print(f"已清理 {removed} 个测试产物")

    # 隔离测试结束后，再看一眼真实配置的链路是否正常（不影响上面的结论）
    try:
        test_live_onebot_if_configured()
    except Exception as exc:
        print(f"  [SKIP] 真实链路检查异常：{exc}")

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
