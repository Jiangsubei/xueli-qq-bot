#!/usr/bin/env python3
import asyncio
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# ---------------------------------------------------------------------------
# Logging configuration - 按模块分层
# ---------------------------------------------------------------------------
_root = logging.getLogger()
_root.setLevel(logging.INFO)
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)

# 各模块详细程度配置
logging.getLogger("src.core.model_invocation_router").setLevel(logging.DEBUG)   # 状态机流水账 → DEBUG
logging.getLogger("src.handlers.reply_pipeline").setLevel(logging.DEBUG)        # FULL PROMPT 详情 → DEBUG
logging.getLogger("src.memory").setLevel(logging.DEBUG)                        # 后台记忆活动 → DEBUG
logging.getLogger("src.core.bootstrap").setLevel(logging.WARNING)                # 启动后静默，只留 WARNING
logging.getLogger("src.services").setLevel(logging.WARNING)                     # AI/图片/视觉服务 WARNING
logging.getLogger("src.adapters").setLevel(logging.WARNING)                    # 协议适配 WARNING
logging.getLogger("websockets").setLevel(logging.WARNING)                      # WebSocket 连接 WARNING
logging.getLogger("jieba").setLevel(logging.WARNING)                          # 结巴分词 WARNING
# ---------------------------------------------------------------------------

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
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass