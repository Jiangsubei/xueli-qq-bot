from __future__ import annotations

import logging
import os
import sys
import threading
from pathlib import Path
from socketserver import ThreadingMixIn
from typing import Optional
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

logger = logging.getLogger(__name__)


class _ThreadedWSGIServer(ThreadingMixIn, WSGIServer):
    daemon_threads = True


class _QuietWSGIRequestHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args) -> None:
        try:
            request_line = str(args[0]) if args else ""
            status_code = int(args[1]) if len(args) > 1 else 0
        except (TypeError, ValueError):
            request_line = ""
            status_code = 0
        if request_line.startswith("GET /api/dashboard/") and 200 <= status_code < 400:
            return
        if request_line.startswith("GET /static/") and 200 <= status_code < 400:
            return
        logger.debug("[WebUI] %s - - [%s] %s", self.address_string(), self.log_date_time_string(), format % args)


class WebUIRuntimeServer:
    def __init__(self, host: str = "127.0.0.1", port: int = 8000):
        self.host = host
        self.port = port
        self._httpd: Optional[WSGIServer] = None
        self._thread: Optional[threading.Thread] = None

    @property
    def display_host(self) -> str:
        if self.host in {"0.0.0.0", "::"}:
            return "127.0.0.1"
        return self.host

    @property
    def display_address(self) -> str:
        return f"{self.display_host}:{self.port}"

    @property
    def display_url(self) -> str:
        return f"http://{self.display_address}/"

    def start(self) -> bool:
        if self._thread and self._thread.is_alive():
            return True

        project_root = Path(__file__).resolve().parent
        project_root_text = str(project_root)
        if project_root_text not in sys.path:
            sys.path.insert(0, project_root_text)

        os.environ.setdefault("DJANGO_SETTINGS_MODULE", "webui_site.settings")

        from django.contrib.staticfiles.handlers import StaticFilesHandler
        from django.core.wsgi import get_wsgi_application

        application = StaticFilesHandler(get_wsgi_application())

        try:
            self._httpd = make_server(
                self.host,
                self.port,
                application,
                server_class=_ThreadedWSGIServer,
                handler_class=_QuietWSGIRequestHandler,
            )
        except OSError as exc:
            logger.warning("[WebUI] WebUI 启动失败，机器人将继续运行")
            self._httpd = None
            self._thread = None
            return False

        self.port = int(getattr(self._httpd, "server_port", self.port))
        self._thread = threading.Thread(target=self._serve_forever, name="webui-server", daemon=True)
        self._thread.start()
            logger.debug("[WebUI] WebUI 地址已就绪")
        return True

    def _serve_forever(self) -> None:
        if self._httpd is None:
            return
        self._httpd.serve_forever()

    def stop(self) -> None:
        if self._httpd is None:
            return
        logger.debug("正在停止 WebUI")
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self._httpd = None
        self._thread = None


def create_webui_runtime_server_from_env() -> WebUIRuntimeServer:
    host = str(os.getenv("WEBUI_HOST", "127.0.0.1") or "127.0.0.1").strip()
    try:
        port = int(str(os.getenv("WEBUI_PORT", "8000") or "8000").strip())
    except ValueError:
        logger.warning("WEBUI_PORT 无效，已回退到 8000")
        port = 8000
    return WebUIRuntimeServer(host=host, port=port)
