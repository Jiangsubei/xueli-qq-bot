#!/usr/bin/env python3
import asyncio
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.runtime_supervisor import BotRuntimeSupervisor
from src.core.webui_runtime_registry import register_runtime_control, unregister_runtime
from src.webui.runtime_server import create_webui_runtime_server_from_env

logger = logging.getLogger(__name__)


async def main():
    supervisor = BotRuntimeSupervisor()
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def handle_shutdown(signum, frame):
        del frame
        logger.info('main received shutdown signal: %s', signum)
        shutdown_event.set()

    try:
        signal.signal(signal.SIGINT, handle_shutdown)
        signal.signal(signal.SIGTERM, handle_shutdown)
    except (AttributeError, ValueError):
        pass

    register_runtime_control(supervisor=supervisor, loop=loop)
    webui_server = create_webui_runtime_server_from_env()
    webui_started = webui_server.start()

    if webui_started:
        logger.info('WebUI URL: %s', webui_server.display_url)

    try:
        await supervisor.start_bot()
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info('keyboard interrupt received in main')
    except Exception as exc:
        print(f'Runtime error: {exc}')
        raise
    finally:
        await supervisor.shutdown()
        unregister_runtime(clear_control=True)
        webui_server.stop()


if __name__ == '__main__':
    if sys.platform == 'win32' and sys.version_info < (3, 16):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
