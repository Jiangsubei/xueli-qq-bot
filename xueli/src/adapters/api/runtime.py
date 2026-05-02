from __future__ import annotations

import json
import logging
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Dict, Optional

from src.adapters.api.adapter import ApiAdapter
from src.core.webui_runtime_registry import get_runtime_state, run_coro_threadsafe

logger = logging.getLogger(__name__)


class ApiRuntimeError(RuntimeError):
    pass


def ingest_api_payload(payload: Dict[str, Any], *, adapter: Optional[ApiAdapter] = None, timeout: float = 10.0) -> None:
    state = get_runtime_state()
    bot = state.get("bot")
    if bot is None:
        raise ApiRuntimeError("bot runtime unavailable")
    api_adapter = adapter or ApiAdapter()
    run_coro_threadsafe(bot.ingest_adapter_payload(payload, adapter=api_adapter), timeout=timeout)


class ApiRuntimeServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8765, *, enabled: bool = False, request_timeout: float = 10.0):
        self.host = host
        self.port = int(port)
        self.enabled = bool(enabled)
        self.request_timeout = float(request_timeout)
        self.adapter = ApiAdapter()
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def display_address(self) -> str:
        display_host = "127.0.0.1" if self.host in {"0.0.0.0", "::"} else self.host
        return f"{display_host}:{self.port}"

    @property
    def display_url(self) -> str:
        return f"http://{self.display_address}/events"

    def start(self) -> bool:
        if not self.enabled:
            return False
        if self._thread and self._thread.is_alive():
            return True

        handler_cls = self._build_handler_class()
        try:
            self._httpd = ThreadingHTTPServer((self.host, self.port), handler_cls)
        except OSError as exc:
            logger.warning("[API入口] API runtime 启动失败，开放 API 入口不可用")
            self._httpd = None
            self._thread = None
            return False

        self.port = int(getattr(self._httpd, "server_port", self.port))
        self._thread = threading.Thread(target=self._serve_forever, name="api-runtime-server", daemon=True)
        self._thread.start()
        logger.info("[API入口] 开放 API 入口已就绪")
        return True

    def stop(self) -> None:
        if self._httpd is None:
            return
        logger.debug("正在停止开放 API runtime")
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._httpd = None
        self._thread = None

    def _serve_forever(self) -> None:
        if self._httpd is None:
            return
        self._httpd.serve_forever()

    def _build_handler_class(self):
        runtime = self

        class _ApiRuntimeRequestHandler(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                if self.path.rstrip("/") != "/health":
                    self._send_json(404, {"ok": False, "error": "not_found"})
                    return
                state = get_runtime_state()
                self._send_json(200, {"ok": True, "bot_ready": state.get("bot") is not None})

            def do_POST(self) -> None:
                if self.path.rstrip("/") != "/events":
                    self._send_json(404, {"ok": False, "error": "not_found"})
                    return
                try:
                    payload = self._read_json_body()
                    ingest_api_payload(payload, adapter=runtime.adapter, timeout=runtime.request_timeout)
                except ApiRuntimeError as exc:
                    self._send_json(503, {"ok": False, "error": str(exc)})
                    return
                except ValueError as exc:
                    self._send_json(400, {"ok": False, "error": str(exc)})
                    return
                except Exception as exc:
                    logger.error("[API入口] 开放 API 入站处理失败")
                    self._send_json(500, {"ok": False, "error": "internal_error"})
                    return
                self._send_json(202, {"ok": True, "accepted": True})

            def log_message(self, format: str, *args: Any) -> None:
                logger.debug("API runtime: " + format, *args)

            def _read_json_body(self) -> Dict[str, Any]:
                try:
                    content_length = int(self.headers.get("Content-Length") or "0")
                except ValueError as exc:
                    raise ValueError("invalid content length") from exc
                if content_length <= 0:
                    raise ValueError("empty request body")
                raw_body = self.rfile.read(content_length)
                try:
                    payload = json.loads(raw_body.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                    raise ValueError("invalid json body") from exc
                if not isinstance(payload, dict):
                    raise ValueError("json body must be an object")
                return payload

            def _send_json(self, status_code: int, payload: Dict[str, Any]) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                self.send_response(status_code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

        return _ApiRuntimeRequestHandler


def create_api_runtime_server_from_env() -> ApiRuntimeServer:
    enabled = str(os.getenv("API_RUNTIME_ENABLED", "false") or "false").strip().lower() in {"1", "true", "yes", "on"}
    host = str(os.getenv("API_RUNTIME_HOST", "127.0.0.1") or "127.0.0.1").strip()
    try:
        port = int(str(os.getenv("API_RUNTIME_PORT", "8765") or "8765").strip())
    except ValueError:
        logger.warning("API_RUNTIME_PORT 无效，已回退到 8765")
        port = 8765
    try:
        request_timeout = float(str(os.getenv("API_RUNTIME_TIMEOUT", "10") or "10").strip())
    except ValueError:
        logger.warning("API_RUNTIME_TIMEOUT 无效，已回退到 10 秒")
        request_timeout = 10.0
    return ApiRuntimeServer(host=host, port=port, enabled=enabled, request_timeout=request_timeout)
