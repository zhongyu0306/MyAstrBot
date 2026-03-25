from __future__ import annotations

import atexit
import json
import mimetypes
import secrets
import socket
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from astrbot.api import logger
from astrbot.api.event import AstrMessageEvent

from .memory_utils import init_user_memory_store
from .passive_memory_utils import init_passive_memory_store


class _ReusableThreadingHTTPServer(ThreadingHTTPServer):
    allow_reuse_address = True
    daemon_threads = True


class MemoryPanelManager:
    def __init__(self, context: Any, config: Any) -> None:
        self.context = context
        self.config = config
        self.host = str(getattr(config, "memory_panel_host", "0.0.0.0") or "0.0.0.0").strip()
        self.port = int(getattr(config, "memory_panel_port", 7835) or 7835)
        self.enabled = bool(getattr(config, "memory_panel_enabled", True))
        self.public_base_url = str(getattr(config, "memory_panel_public_base_url", "") or "").strip()
        self.assets_dir = Path(__file__).with_name("memory_panel_assets")
        self.token = secrets.token_urlsafe(18)
        self._server: _ReusableThreadingHTTPServer | None = None
        self._thread: threading.Thread | None = None
        self._active_port: int | None = None
        self._lock = threading.Lock()
        atexit.register(self._shutdown_quietly)

    def refresh(self, context: Any, config: Any) -> None:
        self.context = context
        self.config = config
        self.enabled = bool(getattr(config, "memory_panel_enabled", True))
        self.host = str(getattr(config, "memory_panel_host", self.host) or self.host).strip()
        self.port = int(getattr(config, "memory_panel_port", self.port) or self.port)
        self.public_base_url = str(getattr(config, "memory_panel_public_base_url", self.public_base_url) or "").strip()

    @staticmethod
    def _normalize_base_url(base_url: str) -> str:
        normalized = str(base_url or "").strip().rstrip("/")
        if not normalized:
            return ""
        if "://" not in normalized:
            normalized = f"http://{normalized}"
        return normalized

    def panel_url(self, *, with_token: bool = True) -> str:
        public_base = self._normalize_base_url(self.public_base_url)
        current_port = self._active_port or self.port
        if public_base:
            base = f"{public_base}/"
        else:
            display_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
            base = f"http://{display_host}:{current_port}/"
        return f"{base}?token={self.token}" if with_token else base

    def _is_port_available(self, port: int) -> bool:
        try:
            with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
                sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                sock.bind((self.host, port))
                return True
        except OSError:
            return False

    def _find_available_port(self, start_port: int, attempts: int = 20) -> int:
        for port in range(start_port, start_port + attempts):
            if self._is_port_available(port):
                return port
        raise RuntimeError(f"记忆面板启动失败：从端口 {start_port} 开始连续 {attempts} 个端口都被占用了。")

    def ensure_started(self) -> str:
        if not self.enabled:
            raise RuntimeError("记忆面板已在配置中关闭，请先在插件配置里开启 `memory_panel_enabled`。")
        with self._lock:
            if self._thread and self._thread.is_alive():
                return self.panel_url()
            handler = self._build_handler()
            target_port = self.port
            public_base = self._normalize_base_url(self.public_base_url)
            if not self._is_port_available(target_port):
                if public_base:
                    raise RuntimeError(
                        f"记忆面板启动失败：端口 {target_port} 已被占用。"
                        f"你当前配置了固定访问地址 {public_base}，请先释放该端口，"
                        "或同时修改 `memory_panel_port` 和 `memory_panel_public_base_url` 后再试。"
                    )
                target_port = self._find_available_port(target_port + 1)
                logger.warning(
                    "[memory_panel] configured port %s is already in use, switched to %s",
                    self.port,
                    target_port,
                )
            try:
                self._server = _ReusableThreadingHTTPServer((self.host, target_port), handler)
            except OSError as exc:
                raise RuntimeError(
                    f"记忆面板启动失败：无法绑定 {self.host}:{target_port}，原因：{exc}"
                ) from exc
            self._active_port = int(getattr(self._server, "server_port", target_port) or target_port)
            self._thread = threading.Thread(target=self._server.serve_forever, daemon=True, name="astrbot_memory_panel")
            self._thread.start()
            logger.info("[memory_panel] started at %s", self.panel_url(with_token=False))
            return self.panel_url()

    def stop(self) -> bool:
        with self._lock:
            if not self._server:
                return False
            try:
                self._server.shutdown()
                self._server.server_close()
            finally:
                self._server = None
                self._active_port = None
            if self._thread:
                self._thread.join(timeout=2.0)
                self._thread = None
            logger.info("[memory_panel] stopped")
            return True

    def status(self) -> dict[str, Any]:
        running = bool(self._thread and self._thread.is_alive())
        return {
            "enabled": self.enabled,
            "running": running,
            "url": self.panel_url() if running else self.panel_url(with_token=False),
        }

    def _shutdown_quietly(self) -> None:
        try:
            self.stop()
        except Exception:
            pass

    def _read_asset(self, relative_path: str) -> bytes:
        asset_path = self.assets_dir / relative_path
        return asset_path.read_bytes()

    def _parse_body(self, handler: BaseHTTPRequestHandler) -> dict[str, Any]:
        length = int(handler.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        raw = handler.rfile.read(length)
        if not raw:
            return {}
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception:
            return {}

    def _build_user_item(self, entry: dict[str, Any], counts: dict[str, dict[str, int]]) -> dict[str, Any]:
        qq_id = str(entry.get("qq_id") or "").strip()
        aliases = entry.get("memory_aliases") or []
        return {
            "qq_id": qq_id,
            "memory_name": aliases[0] if aliases else "",
            "memory_aliases": aliases,
            "platform_name": str(entry.get("platform_name") or "").strip(),
            "note": str(entry.get("note") or "").strip(),
            "last_seen_at": str(entry.get("last_seen_at") or "").strip(),
            "updated_at": str(entry.get("updated_at") or "").strip(),
            "counts": counts.get(qq_id, {"preferences": 0, "relations": 0, "habits": 0, "events": 0}),
        }

    @staticmethod
    def _to_float(value: Any, default: float) -> float:
        try:
            return float(value)
        except (TypeError, ValueError):
            return default

    def _build_user_list(self, query: str = "") -> list[dict[str, Any]]:
        memory_store = init_user_memory_store()
        passive_store = init_passive_memory_store()
        records = memory_store.search_memories(query, limit=100, include_observed_only=True) if query else memory_store.list_all_memories()
        counts = passive_store.get_counts_by_user()
        return [self._build_user_item(entry, counts) for entry in records[:100]]

    def _build_user_detail(self, qq_id: str) -> dict[str, Any]:
        memory_store = init_user_memory_store()
        passive_store = init_passive_memory_store()
        entry = memory_store.get_memory(qq_id) or {"qq_id": qq_id, "memory_aliases": [], "platform_name": "", "note": ""}
        user_item = self._build_user_item(entry, passive_store.get_counts_by_user())
        center_label = user_item["memory_name"] or user_item["platform_name"] or user_item["qq_id"]
        return {
            "user": user_item,
            "preferences": passive_store.list_preferences(qq_id),
            "relations": passive_store.list_relations(qq_id),
            "habits": passive_store.list_habits(qq_id),
            "events": passive_store.list_events(qq_id),
            "graph": passive_store.build_relation_graph(qq_id, center_label=center_label),
        }

    def _overview_payload(self, query: str = "") -> dict[str, Any]:
        memory_store = init_user_memory_store()
        passive_store = init_passive_memory_store()
        return {
            "stats": {
                "users": len(memory_store.list_all_memories()),
                "manual_users": len(memory_store.list_memories()),
                **passive_store.get_dashboard_stats(),
            },
            "users": self._build_user_list(query),
        }

    def _check_token(self, handler: BaseHTTPRequestHandler, query: dict[str, list[str]]) -> bool:
        header_token = str(handler.headers.get("X-Memory-Token") or "").strip()
        query_token = str((query.get("token") or [""])[0]).strip()
        return header_token == self.token or query_token == self.token

    def _json_response(self, handler: BaseHTTPRequestHandler, status: int, payload: dict[str, Any]) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(status)
        handler.send_header("Content-Type", "application/json; charset=utf-8")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _serve_static(self, handler: BaseHTTPRequestHandler, path: str) -> None:
        relative = "index.html" if path == "/" else path.removeprefix("/static/")
        if path != "/" and not path.startswith("/static/"):
            handler.send_error(404)
            return
        try:
            body = self._read_asset(relative)
        except FileNotFoundError:
            handler.send_error(404)
            return
        content_type = "text/html; charset=utf-8" if relative.endswith(".html") else f"{mimetypes.guess_type(relative)[0] or 'application/octet-stream'}"
        handler.send_response(200)
        handler.send_header("Content-Type", content_type)
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    def _handle_api_get(self, path: str, query: dict[str, list[str]]) -> tuple[int, dict[str, Any]]:
        if path == "/api/ping":
            return 200, {"ok": True, "running": True}
        if path == "/api/overview":
            return 200, self._overview_payload(str((query.get("q") or [""])[0]))
        if path == "/api/users":
            return 200, {"users": self._build_user_list(str((query.get("q") or [""])[0]))}
        if path.startswith("/api/users/"):
            qq_id = path.removeprefix("/api/users/").strip("/")
            if "/" in qq_id:
                return 404, {"error": "not_found"}
            return 200, self._build_user_detail(qq_id)
        return 404, {"error": "not_found"}

    def _handle_api_write(self, method: str, path: str, payload: dict[str, Any]) -> tuple[int, dict[str, Any]]:
        memory_store = init_user_memory_store()
        passive_store = init_passive_memory_store()

        if method == "PUT" and path.startswith("/api/users/") and "/" not in path.removeprefix("/api/users/").strip("/"):
            qq_id = path.removeprefix("/api/users/").strip("/")
            memory_store.update_user_profile(
                qq_id,
                note=payload.get("note"),
                platform_name=payload.get("platform_name"),
            )
            return 200, self._build_user_detail(qq_id)

        if path.startswith("/api/users/") and path.endswith("/aliases"):
            qq_id = path.removeprefix("/api/users/").removesuffix("/aliases").strip("/")
            alias = str(payload.get("alias") or "").strip()
            scene_type = str(payload.get("scene_type") or "global").strip()
            scene_value = str(payload.get("scene_value") or "").strip()
            if method == "POST" and alias:
                memory_store.set_memory(
                    qq_id,
                    memory_name=alias,
                    scene_type=scene_type,
                    scene_value=scene_value,
                )
                return 200, self._build_user_detail(qq_id)
            if method == "DELETE" and alias:
                deleted = memory_store.delete_alias(
                    qq_id,
                    alias,
                    scene_type=scene_type,
                    scene_value=scene_value,
                )
                return 200, {"ok": deleted, "detail": self._build_user_detail(qq_id)}

        if path.startswith("/api/scene-aliases/"):
            record_id = int(path.removeprefix("/api/scene-aliases/").strip("/"))
            if method == "PUT":
                record = memory_store.update_scene_alias(
                    record_id,
                    alias=str(payload.get("alias") or "").strip(),
                    scene_type=str(payload.get("scene_type") or "group").strip(),
                    scene_value=str(payload.get("scene_value") or "").strip(),
                )
                if not record:
                    return 400, {"error": "场景别名保存失败，请检查称呼和群号是否填写正确。"}
                return 200, {"record": record}
            deleted, qq_id = memory_store.delete_scene_alias(record_id)
            return 200, {"ok": deleted, "detail": self._build_user_detail(qq_id) if qq_id else None}

        if path.startswith("/api/users/") and path.endswith("/preferences") and method == "POST":
            qq_id = path.removeprefix("/api/users/").removesuffix("/preferences").strip("/")
            record = passive_store.save_preference(
                qq_id,
                str(payload.get("preference_type") or "like").strip(),
                str(payload.get("value") or "").strip(),
                confidence=self._to_float(payload.get("confidence"), 0.82),
                source_text=str(payload.get("source_text") or "记忆面板手动录入").strip(),
            )
            return 200, {"record": record, "detail": self._build_user_detail(qq_id)}

        if path.startswith("/api/users/") and path.endswith("/relations") and method == "POST":
            qq_id = path.removeprefix("/api/users/").removesuffix("/relations").strip("/")
            record = passive_store.save_relation(
                qq_id,
                str(payload.get("target_name") or "").strip(),
                str(payload.get("relation_type") or "").strip(),
                target_qq_id=str(payload.get("target_qq_id") or "").strip(),
                note=str(payload.get("note") or "").strip(),
                confidence=self._to_float(payload.get("confidence"), 0.84),
                source_text=str(payload.get("source_text") or "记忆面板手动录入").strip(),
            )
            return 200, {"record": record, "detail": self._build_user_detail(qq_id)}

        if path.startswith("/api/users/") and path.endswith("/habits") and method == "POST":
            qq_id = path.removeprefix("/api/users/").removesuffix("/habits").strip("/")
            record = passive_store.save_habit(
                qq_id,
                str(payload.get("module_name") or "").strip(),
                str(payload.get("habit_key") or "").strip(),
                str(payload.get("habit_value") or "").strip(),
                source_text=str(payload.get("source_text") or "记忆面板手动录入").strip(),
            )
            return 200, {"record": record, "detail": self._build_user_detail(qq_id)}

        if path.startswith("/api/users/") and path.endswith("/events") and method == "POST":
            qq_id = path.removeprefix("/api/users/").removesuffix("/events").strip("/")
            record = passive_store.save_event(
                qq_id,
                str(payload.get("summary") or "").strip(),
                event_date_label=str(payload.get("event_date_label") or "").strip(),
                confidence=self._to_float(payload.get("confidence"), 0.82),
                source_text=str(payload.get("source_text") or "记忆面板手动录入").strip(),
            )
            return 200, {"record": record, "detail": self._build_user_detail(qq_id)}

        if path.startswith("/api/preferences/"):
            record_id = int(path.removeprefix("/api/preferences/").strip("/"))
            if method == "PUT":
                record = passive_store.update_preference(
                    record_id,
                    value=str(payload.get("value") or "").strip(),
                    preference_type=str(payload.get("preference_type") or "like").strip(),
                    confidence=self._to_float(payload.get("confidence"), 0.82),
                    source_text=str(payload.get("source_text") or "记忆面板手动录入").strip(),
                )
                return 200, {"record": record}
            return 200, {"ok": passive_store.delete_preference(record_id)}

        if path.startswith("/api/relations/"):
            record_id = int(path.removeprefix("/api/relations/").strip("/"))
            if method == "PUT":
                record = passive_store.update_relation(
                    record_id,
                    target_name=str(payload.get("target_name") or "").strip(),
                    relation_type=str(payload.get("relation_type") or "").strip(),
                    target_qq_id=str(payload.get("target_qq_id") or "").strip(),
                    note=str(payload.get("note") or "").strip(),
                    confidence=self._to_float(payload.get("confidence"), 0.84),
                    source_text=str(payload.get("source_text") or "记忆面板手动录入").strip(),
                )
                return 200, {"record": record}
            return 200, {"ok": passive_store.delete_relation(record_id)}

        if path.startswith("/api/habits/"):
            record_id = int(path.removeprefix("/api/habits/").strip("/"))
            if method == "PUT":
                record = passive_store.update_habit(
                    record_id,
                    module_name=str(payload.get("module_name") or "").strip(),
                    habit_key=str(payload.get("habit_key") or "").strip(),
                    habit_value=str(payload.get("habit_value") or "").strip(),
                    source_text=str(payload.get("source_text") or "记忆面板手动录入").strip(),
                )
                return 200, {"record": record}
            return 200, {"ok": passive_store.delete_habit(record_id)}

        if path.startswith("/api/events/"):
            record_id = int(path.removeprefix("/api/events/").strip("/"))
            if method == "PUT":
                record = passive_store.update_event(
                    record_id,
                    summary=str(payload.get("summary") or "").strip(),
                    event_date_label=str(payload.get("event_date_label") or "").strip(),
                    confidence=self._to_float(payload.get("confidence"), 0.82),
                    source_text=str(payload.get("source_text") or "记忆面板手动录入").strip(),
                )
                return 200, {"record": record}
            return 200, {"ok": passive_store.delete_event(record_id)}

        return 404, {"error": "not_found"}

    def _build_handler(self):
        manager = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt: str, *args: Any) -> None:
                logger.debug("[memory_panel] " + fmt, *args)

            def _dispatch(self, method: str) -> None:
                parsed = urlparse(self.path)
                path = parsed.path or "/"
                query = parse_qs(parsed.query or "")
                if path == "/" or path.startswith("/static/"):
                    manager._serve_static(self, path)
                    return
                if not path.startswith("/api/"):
                    self.send_error(404)
                    return
                if not manager._check_token(self, query):
                    manager._json_response(self, 403, {"error": "forbidden"})
                    return
                if method == "GET":
                    status, payload = manager._handle_api_get(path, query)
                else:
                    status, payload = manager._handle_api_write(method, path, manager._parse_body(self))
                manager._json_response(self, status, payload)

            def do_GET(self) -> None:
                self._dispatch("GET")

            def do_POST(self) -> None:
                self._dispatch("POST")

            def do_PUT(self) -> None:
                self._dispatch("PUT")

            def do_DELETE(self) -> None:
                self._dispatch("DELETE")

        return Handler


_memory_panel_manager: MemoryPanelManager | None = None


def init_memory_panel_manager(context: Any, config: Any) -> MemoryPanelManager:
    global _memory_panel_manager
    if _memory_panel_manager is None:
        _memory_panel_manager = MemoryPanelManager(context, config)
    else:
        _memory_panel_manager.refresh(context, config)
    return _memory_panel_manager


def maybe_autostart_memory_panel(context: Any, config: Any) -> None:
    if not bool(getattr(config, "memory_panel_enabled", True)):
        return
    if not bool(getattr(config, "memory_panel_auto_start", False)):
        return
    try:
        init_memory_panel_manager(context, config).ensure_started()
    except Exception as exc:
        logger.warning("[memory_panel] auto start failed: %s", exc)


async def handle_memory_panel_command(event: AstrMessageEvent, context: Any, config: Any):
    store = init_user_memory_store()
    store.observe_user(event)
    if not store._is_admin_event(event):
        yield event.plain_result("记忆面板仅允许管理员使用。")
        return

    msg = store._safe_message_text(event)
    parts = msg.split()
    action = str(parts[1] if len(parts) > 1 else "打开").strip().lower()
    manager = init_memory_panel_manager(context, config)

    if action in {"关闭", "stop", "close"}:
        stopped = manager.stop()
        yield event.plain_result("记忆面板已关闭。" if stopped else "记忆面板当前没有在运行。")
        return

    if action in {"状态", "status"}:
        status = manager.status()
        text = "记忆面板运行中" if status["running"] else "记忆面板未启动"
        yield event.plain_result(f"{text}\n{status['url']}")
        return

    try:
        url = manager.ensure_started()
    except Exception as exc:
        yield event.plain_result(f"记忆面板启动失败：{exc}")
        return

    yield event.plain_result(
        "记忆面板已启动。\n"
        f"{url}\n"
        "这里可以查看和修改认人记忆、偏好记忆、关系图谱、跨模块习惯和事件回忆。"
    )
