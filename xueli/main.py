#!/usr/bin/env python3
import asyncio
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.runtime_supervisor import BotRuntimeSupervisor
from src.core.webui_runtime_registry import register_runtime_control, unregister_runtime
from src.adapters.api.runtime import create_api_runtime_server_from_env
from src.webui.runtime_server import create_webui_runtime_server_from_env

logger = logging.getLogger(__name__)


def _log_webui_entry(address: str) -> None:
    logger.info("---------------------------------")
    logger.info("管理页面地址：%s", address)
    logger.info("---------------------------------")


async def main():
    supervisor = BotRuntimeSupervisor()
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def handle_shutdown(signum, frame):
        del frame
        logger.info("主程序收到退出信号：%s", signum)
        shutdown_event.set()

    try:
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
    except (AttributeError, ValueError):
        pass

    register_runtime_control(supervisor=supervisor, loop=loop)
    webui_server = create_webui_runtime_server_from_env()
    webui_started = webui_server.start()
    api_runtime_server = create_api_runtime_server_from_env()
    api_runtime_started = api_runtime_server.start()

    try:
        await supervisor.start_bot()
        if webui_started:
            _log_webui_entry(webui_server.display_address)
        if api_runtime_started:
            logger.info("开放 API 地址：%s", api_runtime_server.display_url)
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("主程序收到键盘中断，准备退出")
    except Exception as exc:
        print(f"Runtime error: {exc}")
        raise
    finally:
        await supervisor.shutdown()
        unregister_runtime(clear_control=True)
        api_runtime_server.stop()
        webui_server.stop()


if __name__ == "__main__":
    if sys.platform == "win32" and sys.version_info < (3, 16):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
