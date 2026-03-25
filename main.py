#!/usr/bin/env python3
"""
QQ 机器人入口

使用方法:
1. 确保 NapCat 已启动并配置好 WebSocket (默认 ws://127.0.0.1:6700)
2. 配置环境变量或 .env 文件：
   - DEEPSEEK_API_KEY=your_api_key
3. 运行: python main.py
"""
import asyncio
import sys
import os

# 将当前目录添加到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.bot import QQBot


async def main():
    """主函数"""
    bot = QQBot()

    try:
        await bot.run()
    except KeyboardInterrupt:
        print("\n收到键盘中断，正在关闭...")
    except Exception as e:
        print(f"运行出错: {e}")
        raise


if __name__ == "__main__":
    # Windows 上设置合适的 event loop 策略 (Python < 3.16)
    if sys.platform == "win32" and sys.version_info < (3, 16):
        try:
            asyncio.set_event_loop_policy(asyncio.WindowsSelectorEventLoopPolicy())
        except AttributeError:
            # Python 3.16+ 已弃用此 API
            pass

    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass