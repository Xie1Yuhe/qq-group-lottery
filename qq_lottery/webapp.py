"""界面用的本地 HTTP 服务端。

设计要点：
  · 只绑定 127.0.0.1，不对外网开放
  · 每次启动生成一次性令牌，页面通过 Cookie / 请求头携带，防止其他网页调用本机接口
  · 校验 Host 只允许回环地址，防 DNS 重绑定
  · 接口统一返回 JSON，业务逻辑全部委托给 LotteryService
"""

from __future__ import annotations

import json
import mimetypes
import secrets
import socket
import threading
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, unquote, urlparse

from . import paths
from .service import LotteryService, Result

WEB_DIR = paths.project_root() / "webapp"
# 图标文件放在项目根的 assets/ 下（与 webapp/ 同级）。
# 页面 favicon 决定 Edge 应用窗口在任务栏里显示的图标，所以这个路由是必需的不是装饰。
ICON_DIR = paths.project_root() / "assets"
ICON_FILES = {
    "/icon.ico": "icon.ico",
    "/favicon.ico": "icon.ico",
    "/icon-32.png": "icon-32.png",
    "/icon-180.png": "icon-180.png",
}
TOKEN_HEADER = "X-Lottery-Token"
COOKIE_NAME = "lottery_token"

ALLOWED_DOWNLOAD_ROOTS = ("out", "data")


class ApiError(Exception):
    def __init__(self, status: int, message: str, code: str = "error") -> None:
        super().__init__(message)
        self.status = status
        self.message = message
        self.code = code


class LotteryHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address: tuple[str, int], handler: type[BaseHTTPRequestHandler], service: LotteryService, token: str) -> None:
        super().__init__(address, handler)
        self.service = service
        self.token = token


class Handler(BaseHTTPRequestHandler):
    server_version = "QQLotteryUI/1.0"
    protocol_version = "HTTP/1.1"

    # ------------------------------------------------------------ 基础
    def log_message(self, fmt: str, *args: Any) -> None:  # 静默访问日志
        pass

    @property
    def app(self) -> LotteryHTTPServer:
        return self.server  # type: ignore[return-value]

    def _send(self, status: int, body: bytes, content_type: str, extra: dict[str, str] | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json(self, payload: dict[str, Any], status: int = 200, extra: dict[str, str] | None = None) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self._send(status, body, "application/json; charset=utf-8", extra)

    def _send_error_json(self, status: int, message: str, code: str = "error") -> None:
        self._send_json({"ok": False, "message": message, "code": code, "data": None}, status=status)

    # ------------------------------------------------------------ 校验
    def _host_ok(self) -> bool:
        host = (self.headers.get("Host") or "").split(":")[0].strip("[]").lower()
        return host in ("127.0.0.1", "localhost", "::1", "")

    def _origin_ok(self) -> bool:
        """拒绝来自其他站点的跨站请求（配合令牌形成双重防护）。"""
        origin = (self.headers.get("Origin") or "").strip()
        if origin:
            allowed = {
                f"http://127.0.0.1:{self.app.server_address[1]}",
                f"http://localhost:{self.app.server_address[1]}",
            }
            if origin not in allowed:
                return False
        # 现代浏览器会带上这个头；同源请求为 same-origin
        site = (self.headers.get("Sec-Fetch-Site") or "").strip().lower()
        if site in ("cross-site", "same-site"):
            return False
        return True

    def _token_from_request(self, query: dict[str, list[str]]) -> str:
        header = self.headers.get(TOKEN_HEADER) or ""
        if header:
            return header.strip()
        cookie = self.headers.get("Cookie") or ""
        for part in cookie.split(";"):
            name, _, value = part.strip().partition("=")
            if name == COOKIE_NAME:
                return value
        return (query.get("token") or [""])[0]

    def _check_token(self, query: dict[str, list[str]]) -> bool:
        supplied = self._token_from_request(query)
        return bool(supplied) and secrets.compare_digest(supplied, self.app.token)

    # ------------------------------------------------------------ GET
    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if not self._host_ok():
            return self._send_error_json(HTTPStatus.FORBIDDEN, "非法的请求来源", "bad_host")
        if not self._origin_ok():
            return self._send_error_json(HTTPStatus.FORBIDDEN, "拒绝跨站请求", "bad_origin")

        # 图标资源（页面标签 / 任务栏 / 窗口都从这里取，不需要令牌）
        icon = ICON_FILES.get(path)
        if icon is not None:
            target = ICON_DIR / icon
            if target.is_file():
                ctype = "image/x-icon" if icon.endswith(".ico") else "image/png"
                return self._send(HTTPStatus.OK, target.read_bytes(), ctype)
            return self._send_error_json(HTTPStatus.NOT_FOUND, f"图标缺失：{icon}", "not_found")

        if path in ("/", "/index.html"):
            return self._serve_index()

        if path.startswith("/api/"):
            if not self._check_token(query):
                return self._send_error_json(HTTPStatus.FORBIDDEN, "会话令牌无效，请重新打开程序", "bad_token")
            route = path[len("/api/") :]
            if route == "config":
                return self._json(self.app.service.get_config())
            if route == "history":
                limit = int((query.get("limit") or ["50"])[0] or 50)
                return self._json(self.app.service.history(limit))
            if route == "members":
                return self._json(self.app.service.list_group_members())
            if route == "bot/state":
                return self._json(self.app.service.bot_login_state())
            if route == "bot/qr":
                return self._json(self.app.service.bot_qr())
            if route == "bot/info":
                return self._json(self.app.service.bot_info())
            if route == "archive/status":
                return self._json(self.app.service.archive_status())
            if route == "export/download":
                return self._download(query)
            return self._send_error_json(HTTPStatus.NOT_FOUND, f"未知接口 {route}", "not_found")

        return self._serve_static(path)

    def _render_asset(self, path: Path) -> bytes:
        """读取静态文件；含令牌占位符的文件（如 app.js）会就地注入本次会话令牌。"""
        body = path.read_bytes()
        if b"__LOTTERY_TOKEN__" in body:
            body = body.replace(b"__LOTTERY_TOKEN__", self.app.token.encode("ascii"))
        return body

    def _serve_index(self) -> None:
        index = WEB_DIR / "index.html"
        if not index.is_file():
            return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, "界面文件缺失：webapp/index.html", "missing_ui")
        body = self._render_asset(index)
        cookie = f"{COOKIE_NAME}={self.app.token}; Path=/; SameSite=Lax; HttpOnly"
        self._send(HTTPStatus.OK, body, "text/html; charset=utf-8", {"Set-Cookie": cookie})

    def _serve_static(self, path: str) -> None:
        relative = path.lstrip("/")
        if not relative:
            relative = "index.html"
        target = (WEB_DIR / relative).resolve()
        try:
            target.relative_to(WEB_DIR.resolve())
        except ValueError:
            return self._send_error_json(HTTPStatus.FORBIDDEN, "非法路径", "bad_path")
        if not target.is_file():
            return self._send_error_json(HTTPStatus.NOT_FOUND, f"文件不存在：{relative}", "not_found")
        ctype, _ = mimetypes.guess_type(str(target))
        self._send(HTTPStatus.OK, self._render_asset(target), ctype or "application/octet-stream")

    def _download(self, query: dict[str, list[str]]) -> None:
        name = (query.get("name") or [""])[0]
        if not name:
            return self._send_error_json(HTTPStatus.BAD_REQUEST, "缺少文件名", "bad_request")
        root = paths.project_root()
        candidate = (root / name).resolve()
        # 只允许下载程序目录下 out/ 与 data/ 里的文件
        if not any(_inside(candidate, root / allowed) for allowed in ALLOWED_DOWNLOAD_ROOTS):
            return self._send_error_json(HTTPStatus.FORBIDDEN, "不允许下载该路径", "bad_path")
        if not candidate.is_file():
            return self._send_error_json(HTTPStatus.NOT_FOUND, f"文件不存在：{name}", "not_found")
        ctype, _ = mimetypes.guess_type(str(candidate))
        disposition = "attachment" if candidate.suffix.lower() in (".csv", ".json") else "inline"
        self._send(
            HTTPStatus.OK,
            candidate.read_bytes(),
            ctype or "application/octet-stream",
            {"Content-Disposition": f"{disposition}; filename*=UTF-8''{_quote_filename(candidate.name)}"},
        )

    # ------------------------------------------------------------ POST
    def do_POST(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path = unquote(parsed.path)
        query = parse_qs(parsed.query)

        if not self._host_ok():
            return self._send_error_json(HTTPStatus.FORBIDDEN, "非法的请求来源", "bad_host")
        if not self._origin_ok():
            return self._send_error_json(HTTPStatus.FORBIDDEN, "拒绝跨站请求", "bad_origin")
        if not self._check_token(query):
            return self._send_error_json(HTTPStatus.FORBIDDEN, "会话令牌无效，请重新打开程序", "bad_token")
        if not path.startswith("/api/"):
            return self._send_error_json(HTTPStatus.NOT_FOUND, "未知接口", "not_found")

        try:
            payload = self._read_json()
        except ApiError as exc:
            return self._send_error_json(exc.status, exc.message, exc.code)

        route = path[len("/api/") :]
        service = self.app.service
        routes: dict[str, Callable[[dict[str, Any]], Any]] = {
            "config/save": service.save_config,
            "config/test": lambda _p: service.check_config(),
            "stats": lambda p: service.stats(force_reload=bool(p.get("reload"))),
            "draw": service.draw,
            "draw-all-members": service.draw_all_members,
            "post": lambda p: service.post_to_group(p.get("text")),
            "export": lambda p: service.export(str(p.get("kind") or "csv")),
            "snapshot": lambda _p: Result(data=service.snapshot()),
            # 机器人管理
            "bot/start": lambda _p: service.bot_start(),
            "bot/stop": lambda _p: service.bot_stop(),
            "bot/groups": lambda _p: service.bot_groups(),
            "archive/status": lambda _p: service.archive_status(),
            "archive/sync": lambda _p: service.sync_archive(),
            "bot/run-all": service.run_all,
        }
        handler = routes.get(route)
        if handler is None:
            return self._send_error_json(HTTPStatus.NOT_FOUND, f"未知接口 {route}", "not_found")
        try:
            result = handler(payload)
        except Exception as exc:  # 兜底：任何意外都要以 JSON 返回，界面才不会静默失败
            return self._send_error_json(HTTPStatus.INTERNAL_SERVER_ERROR, f"服务端异常：{exc}", "internal_error")
        return self._json(result)

    def _read_json(self) -> dict[str, Any]:
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > 64 * 1024 * 1024:
            raise ApiError(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "请求体过大", "too_large")
        raw = self.rfile.read(length)
        try:
            data = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ApiError(HTTPStatus.BAD_REQUEST, f"请求体不是合法 JSON：{exc}", "bad_json") from exc
        if not isinstance(data, dict):
            raise ApiError(HTTPStatus.BAD_REQUEST, "请求体必须是对象", "bad_json")
        return data

    def _json(self, result: Any) -> None:
        payload = result.as_dict() if hasattr(result, "as_dict") else result
        status = HTTPStatus.OK if payload.get("ok", True) else HTTPStatus.BAD_REQUEST
        self._send_json(payload, status=status)


def _quote_filename(name: str) -> str:
    from urllib.parse import quote

    return quote(name, safe="")


def _inside(path: Path, root: Path) -> bool:
    try:
        path.relative_to(root.resolve())
        return True
    except ValueError:
        return False


def find_free_port(preferred: int = 8791) -> int:
    """优先用固定端口，被占用时让系统分配一个空闲端口。"""
    for candidate in (preferred, 0):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                sock.bind(("127.0.0.1", candidate))
            except OSError:
                continue
            return sock.getsockname()[1]
    raise RuntimeError("找不到可用端口")


def create_server(
    service: LotteryService | None = None,
    port: int = 8791,
) -> tuple[LotteryHTTPServer, str]:
    service = service or LotteryService()
    token = secrets.token_urlsafe(24)
    actual_port = find_free_port(port)
    server = LotteryHTTPServer(("127.0.0.1", actual_port), Handler, service, token)
    return server, token


def serve_forever(server: LotteryHTTPServer, on_ready: Callable[[str], None] | None = None) -> None:
    url = f"http://127.0.0.1:{server.server_address[1]}/?token={server.token}"
    if on_ready:
        on_ready(url)
    server.serve_forever()


def run_in_thread(server: LotteryHTTPServer) -> threading.Thread:
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    return thread
