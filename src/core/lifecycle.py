"""生命周期工具，统一处理资源关闭和任务取消。"""
from __future__ import annotations

import asyncio
import logging
from typing import Any, Iterable, Optional

logger = logging.getLogger(__name__)


async def close_resource(resource: Any, *, label: Optional[str] = None) -> None:
    """Best-effort 关闭资源，支持同步和异步 close。"""
    if resource is None:
        return

    close_method = getattr(resource, "close", None)
    if close_method is None:
        return

    try:
        result = close_method()
        if asyncio.iscoroutine(result):
            await result
    except Exception as exc:
            logger.warning("关闭资源失败：%s，错误=%s", label or type(resource).__name__, exc, exc_info=True)


async def cancel_task(task: Optional[asyncio.Task], *, label: str = "task") -> None:
    """取消单个任务并等待其退出。"""
    if task is None:
        return
    if task.done():
        try:
            task.result()
        except asyncio.CancelledError:
            return
        except Exception as exc:
            logger.warning("任务退出异常：%s，错误=%s", label, exc, exc_info=True)
        return

    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        return
    except Exception as exc:
        logger.warning("取消任务失败：%s，错误=%s", label, exc, exc_info=True)


async def cancel_tasks(tasks: Iterable[asyncio.Task], *, label: str = "tasks") -> None:
    """取消一组任务并等待收尾。"""
    pending = [task for task in tasks if task is not None]
    if not pending:
        return

    logger.info("正在取消后台任务：%s，数量=%s", label, len(pending))
    for task in pending:
        task.cancel()
    await asyncio.gather(*pending, return_exceptions=True)

