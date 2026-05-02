from __future__ import annotations

import asyncio
import inspect
import json
import logging
import time
from typing import Any, Callable, Dict, Optional

import websockets
from websockets.exceptions import ConnectionClosed
from websockets.server import WebSocketServerProtocol

logger = logging.getLogger(__name__)


class NapCatConnection:
    """NapCat WebSocket server transport."""

    def __init__(
        self,
        host: str = None,
        port: int = None,
        on_message: Optional[Callable[[Dict[str, Any]], None]] = None,
        on_connect: Optional[Callable[[], None]] = None,
        on_disconnect: Optional[Callable[[], None]] = None,
    ):
        self.host = host or "0.0.0.0"
        self.port = port or 8095
        self.on_message = on_message
        self.on_connect = on_connect
        self.on_disconnect = on_disconnect

        self.websocket: Optional[WebSocketServerProtocol] = None
        self.server = None
        self._running = False
        self._connected = False
        self._heartbeat_interval = 30
        self._last_heartbeat = 0

    async def start_server(self):
        logger.info("启动 WebSocket 服务：ws://%s:%s", self.host, self.port)

        try:
            self.server = await websockets.serve(
                self._handle_connection,
                self.host,
                self.port,
                ping_interval=None,
                ping_timeout=None,
            )
            logger.info("WebSocket 服务已启动：ws://%s:%s", self.host, self.port)
            logger.info("等待 NapCat 连接")
            await self.server.wait_closed()

        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("WebSocket 服务启动失败：%s", e)
            raise

    async def _handle_connection(self, websocket: WebSocketServerProtocol, path: str = None):
        del path
        client_addr = f"{websocket.remote_address[0]}:{websocket.remote_address[1]}"
        logger.info("NapCat 已连接：%s", client_addr)

        if self.websocket is not None and self.websocket != websocket:
            logger.warning("检测到旧连接，准备断开")
            try:
                await self.websocket.close()
            except ConnectionClosed:
                logger.debug("旧连接关闭时已断开")
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.debug("关闭旧连接时出错：%s", e)

        self.websocket = websocket
        self._connected = True

        if self.on_connect:
            await self._safe_callback(self.on_connect)

        try:
            await self._receive_loop()
        except ConnectionClosed:
            logger.debug("WebSocket 连接已关闭")
        except Exception as e:
            logger.error("接收消息失败：%s", e)
        finally:
            logger.info("NapCat 已断开：%s", client_addr)
            self._connected = False
            self.websocket = None

            if self.on_disconnect:
                await self._safe_callback(self.on_disconnect)

    async def _receive_loop(self):
        while self._connected and self.websocket:
            try:
                message = await asyncio.wait_for(self.websocket.recv(), timeout=1.0)
                await self._handle_message(message)

            except asyncio.TimeoutError:
                await self._check_heartbeat()
                continue

    async def _handle_message(self, message: str):
        try:
            data = json.loads(message)
            logger.debug("收到事件: post_type=%s", data.get("post_type"))

            if data.get("post_type") == "meta_event":
                await self._handle_meta_event(data)
                return

            if self.on_message:
                await self._safe_callback(self.on_message, data)

        except json.JSONDecodeError as e:
            logger.error("事件 JSON 解析失败：%s", e)
        except Exception as e:
            logger.error("处理事件失败：%s", e)

    async def _handle_meta_event(self, data: Dict[str, Any]):
        meta_event_type = data.get("meta_event_type")
        if meta_event_type == "heartbeat":
            logger.debug("收到心跳")
        elif meta_event_type == "lifecycle":
            sub_type = data.get("sub_type")
            logger.debug("生命周期事件：%s", sub_type)

    async def _check_heartbeat(self):
        now = time.time()
        if now - self._last_heartbeat >= self._heartbeat_interval:
            self._last_heartbeat = now

    async def send(self, data: Dict[str, Any]) -> bool:
        if not self.websocket or not self._connected:
            logger.warning("发送失败：WebSocket 未连接")
            return False

        try:
            json_str = json.dumps(data, ensure_ascii=False)
            await self.websocket.send(json_str)
            return True
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("发送消息失败：%s", e)
            return False

    async def disconnect(self):
        self._running = False
        self._connected = False

        if self.websocket:
            try:
                await self.websocket.close()
            except Exception as e:
                logger.debug("关闭连接时出错：%s", e)
            finally:
                self.websocket = None

        if self.server:
            try:
                self.server.close()
                await self.server.wait_closed()
            except Exception as e:
                logger.debug("关闭服务时出错：%s", e)

        logger.info("WebSocket 服务已关闭")

    async def run(self):
        self._running = True
        await self.start_server()

    async def _safe_callback(self, callback: Callable, *args, **kwargs):
        try:
            if inspect.iscoroutinefunction(callback):
                await callback(*args, **kwargs)
            else:
                callback(*args, **kwargs)
        except asyncio.CancelledError:
            raise
        except Exception as e:
            logger.error("回调执行失败：%s", e)
