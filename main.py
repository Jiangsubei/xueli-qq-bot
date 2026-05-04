#!/usr/bin/env python3
import asyncio
import logging
import os
import signal
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "xueli"))

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

logger = logging.getLogger(__name__)
# ---------------------------------------------------------------------------

from src.core.runtime_supervisor import BotRuntimeSupervisor
from src.core.webui_runtime_registry import register_runtime_control, unregister_runtime
from src.adapters.api.runtime import create_api_runtime_server_from_env
from src.webui.runtime_server import create_webui_runtime_server_from_env

logger = logging.getLogger(__name__)


def _log_webui_entry(address: str) -> None:
    logger.info("[启动] 管理页面已就绪")


async def main():
    supervisor = BotRuntimeSupervisor()
    loop = asyncio.get_running_loop()
    shutdown_event = asyncio.Event()

    def handle_shutdown(signum, frame):
        del frame
        logger.info("[启动] 收到退出信号")
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
            logger.info("[启动] 开放 API 已就绪")
        await shutdown_event.wait()
    except KeyboardInterrupt:
        logger.info("[启动] 键盘中断，准备退出")
    except Exception as exc:
        logger.error("[启动] 运行时异常", exc_info=exc)
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