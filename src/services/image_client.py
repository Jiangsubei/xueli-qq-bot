"""
图片下载客户端
支持从 WebSocket 消息中的 URL 直接下载图片
无需依赖 NapCat HTTP API
"""
import aiohttp
import base64
import binascii
import logging
from typing import Optional, Dict, Any

logger = logging.getLogger(__name__)


class ImageClient:
    """图片下载客户端 - 纯 WebSocket 方式"""

    def __init__(self):
        """初始化图片客户端"""
        self.session: Optional[aiohttp.ClientSession] = None

    async def __aenter__(self):
        """异步上下文管理器入口"""
        await self._init_session()
        return self

    async def __aexit__(self, exc_type, exc_val, exc_tb):
        """异步上下文管理器出口"""
        await self.close()

    async def _init_session(self):
        """初始化 HTTP 会话"""
        if not self.session:
            self.session = aiohttp.ClientSession()
            logger.debug("图片客户端会话已创建")

    async def close(self):
        """关闭 HTTP 会话"""
        if self.session:
            await self.session.close()
            self.session = None
            logger.debug("图片客户端会话已关闭")

    def _fix_url(self, url: str) -> str:
        """
        修复 URL 中的转义字符

        Args:
            url: 原始 URL

        Returns:
            修复后的 URL
        """
        # 反转义 &amp; 为 &
        return url.replace("&amp;", "&")

    async def download_image_from_url(self, url: str) -> Optional[bytes]:
        """
        从 URL 下载图片

        Args:
            url: 图片 URL（来自 WebSocket 消息）

        Returns:
            图片二进制数据，下载失败返回 None
        """
        await self._init_session()

        # 修复 URL 中的转义字符
        fixed_url = self._fix_url(url)

        try:
            logger.debug(f"下载图片: {fixed_url[:80]}")
            async with self.session.get(fixed_url, timeout=aiohttp.ClientTimeout(total=30)) as response:
                if response.status != 200:
                    logger.error(f"图片下载失败: HTTP {response.status}")
                    return None

                # 检查内容类型是否为图片
                content_type = response.headers.get('Content-Type', '')
                if not content_type.startswith('image/'):
                    logger.warning(f"返回内容不是图片: {content_type}")

                image_bytes = await response.read()
                logger.debug(f"图片下载完成: {len(image_bytes)} 字节")
                return image_bytes

        except aiohttp.ClientError as e:
            logger.error(f"图片下载请求失败: {e}")
            return None
        except asyncio.TimeoutError:
            logger.error("图片下载超时")
            return None
        except Exception as e:
            logger.error(f"图片下载失败: {e}", exc_info=True)
            return None

    async def download_image_from_base64(self, base64_data: str) -> Optional[bytes]:
        """
        从 base64 编码的数据解码图片

        Args:
            base64_data: base64 编码的图片数据（可以包含或不包含 base64:// 前缀）

        Returns:
            图片二进制数据，解码失败返回 None
        """
        try:
            # 移除可能的 base64:// 前缀
            if base64_data.startswith("base64://"):
                base64_data = base64_data[9:]

            image_bytes = base64.b64decode(base64_data)
            logger.debug(f"Base64 解码完成: {len(image_bytes)} 字节")
            return image_bytes

        except (binascii.Error, ValueError) as e:
            logger.error(f"Base64 解码失败: {e}")
            return None

    async def process_image_segment(self, segment_data: Dict[str, Any]) -> Optional[str]:
        """
        处理图片消息段，下载或解码图片并返回 base64 编码

        这是主要的图片处理方法，自动判断并使用以下方式:
        1. 优先使用 data.url - 从 URL 下载图片（WebSocket 消息中最常见）
        2. 其次使用 data.file - 处理 base64:// 编码或本地路径

        Args:
            segment_data: 图片消息段的数据字段

        Returns:
            base64 编码的图片数据（不含 data URL 前缀），失败返回 None
        """
        try:
            # 优先使用 url（最常见）
            url = segment_data.get("url")
            if url:
                logger.debug(f"按 URL 处理图片: {url[:80]}")
                image_bytes = await self.download_image_from_url(url)
                if image_bytes:
                    return base64.b64encode(image_bytes).decode('utf-8')
                return None

            # 其次使用 file 字段
            file_field = segment_data.get("file")
            if file_field:
                # 检查是否是 base64 编码
                if file_field.startswith("base64://"):
                    logger.debug("按 Base64 处理图片")
                    image_bytes = await self.download_image_from_base64(file_field)
                    if image_bytes:
                        return base64.b64encode(image_bytes).decode('utf-8')
                else:
                    # 本地文件路径 - 直接读取
                    logger.debug(f"读取本地图片: {file_field}")
                    try:
                        with open(file_field, 'rb') as f:
                            image_bytes = f.read()
                        return base64.b64encode(image_bytes).decode('utf-8')
                    except OSError as e:
                        logger.error(f"读取本地图片失败: {e}")
                return None

            logger.warning("图片消息缺少 url 或 file 字段")
            return None

        except (aiohttp.ClientError, asyncio.TimeoutError, OSError, ValueError, binascii.Error) as e:
            logger.error(f"处理图片消息失败: {e}", exc_info=True)
            return None
        except Exception as e:
            logger.error(f"处理图片消息失败: {e}", exc_info=True)
            return None
