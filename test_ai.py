#!/usr/bin/env python3
"""
AI 客户端测试脚本
用于测试 DeepSeek/OpenAI/OpenRouter API 是否配置正确
"""
import asyncio
import sys
import os

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from config import config
from ai_client import AIClient


async def test_ai():
    """测试 AI API"""
    print("=" * 60)
    print("AI API 测试工具")
    print("=" * 60)
    print()

    # 检查配置并确定使用哪个 provider
    provider = "unknown"
    api_key = None
    api_url = None
    model = None
    reasoning_enabled = False

    if config.OPENROUTER_API_KEY:
        provider = "openrouter"
        api_key = config.OPENROUTER_API_KEY
        api_url = config.OPENROUTER_API_URL
        model = config.OPENROUTER_MODEL
        reasoning_enabled = config.OPENROUTER_REASONING_ENABLED
        print(f"✓ 检测到 OpenRouter 配置")
    elif config.DEEPSEEK_API_KEY:
        provider = "deepseek"
        api_key = config.DEEPSEEK_API_KEY
        api_url = config.DEEPSEEK_API_URL
        model = config.DEEPSEEK_MODEL
        print(f"✓ 检测到 DeepSeek 配置")
    elif config.OPENAI_API_KEY:
        provider = "openai"
        api_key = config.OPENAI_API_KEY
        api_url = config.OPENAI_API_URL
        model = config.OPENAI_MODEL
        print(f"✓ 检测到 OpenAI 配置")
    else:
        print("❌ 错误: 未找到任何 API Key 配置")
        print("请在 .env 文件中设置 OPENROUTER_API_KEY 或 DEEPSEEK_API_KEY 或 OPENAI_API_KEY")
        return

    print(f"✓ API Key: {api_key[:10]}...")
    print(f"✓ API URL: {api_url}")
    print(f"✓ 模型: {model}")
    if provider == "openrouter":
        print(f"✓ Reasoning 启用: {reasoning_enabled}")
    print()

    # 创建 AI 客户端
    async with AIClient(
        api_key=api_key,
        api_url=api_url,
        model=model,
        provider=provider
    ) as client:
        print("正在测试 API 连接...")
        print()

        # 准备测试消息
        messages = [
            {"role": "system", "content": "你是一个友好的助手，请简洁地回答。"},
            {"role": "user", "content": "你好！请用一句话介绍自己。"}
        ]

        try:
            # 发送请求
            start_time = asyncio.get_event_loop().time()
            response = await client.chat_completion(
                messages=messages,
                temperature=0.7
            )
            elapsed_time = asyncio.get_event_loop().time() - start_time

            # 显示结果
            print("=" * 60)
            print("✅ API 测试成功!")
            print("=" * 60)
            print()
            print(f"⏱️ 响应时间: {elapsed_time:.2f} 秒")
            print(f"🤖 使用模型: {response.model}")
            print(f"📡 Provider: {client.provider}")
            if response.usage:
                print(f"📝 Token 使用: {response.usage}")
            print()
            print("📝 AI 回复:")
            print("-" * 60)
            print(response.content)
            print("-" * 60)
            print()

            # 如果有 reasoning 内容，显示出来
            if response.reasoning_content:
                print("🧠 Reasoning 内容:")
                print("-" * 60)
                print(response.reasoning_content)
                print("-" * 60)
                print()

            if response.reasoning_details:
                print(f"📋 Reasoning Details: {len(response.reasoning_details)} 条")
                for i, detail in enumerate(response.reasoning_details[:3], 1):  # 只显示前3条
                    print(f"  {i}. {detail}")
                if len(response.reasoning_details) > 3:
                    print(f"  ... 还有 {len(response.reasoning_details) - 3} 条")
                print()

            print("✅ 配置正确！可以启动机器人了。")
            print("运行: python main.py")

        except Exception as e:
            print("=" * 60)
            print("❌ API 测试失败!")
            print("=" * 60)
            print()
            print(f"错误信息: {e}")
            print()
            print("请检查:")
            print("1. API Key 是否正确")
            print("2. 网络连接是否正常")
            print("3. API 账户是否有余额")
            print()


if __name__ == "__main__":
    try:
        asyncio.run(test_ai())
    except KeyboardInterrupt:
        print("\n测试已取消")
    except Exception as e:
        print(f"运行出错: {e}")