#!/usr/bin/env python3
"""
Claude QQ Bot 启动入口。

运行方式:
1. 准备并检查仓库根目录下的 config.json。
2. 确认 NapCat WebSocket 服务可连接。
3. 执行: python main.py

启动链路:
- main.py 负责事件循环与 QQBot.run()
- QQBot 负责运行时协调与统一关闭
- BotBootstrapper 负责配置校验、依赖装配、Memory 初始化和连接创建
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.bot import QQBot


async def main():
    """运行机器人主循环。"""
    bot = QQBot()

    try:
        await bot.run()
    except KeyboardInterrupt:
        print("\n收到键盘中断，正在关闭...")
    except Exception as e:
        print(f"运行出错: {e}")
        raise


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
