#!/usr/bin/env python3
"""
通用 OpenAI 兼容 API 测试脚本
支持测试任意 OpenAI 兼容服务
"""
import asyncio
import sys
import os

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import config
from ai_client import AIClient


def print_config():
    """打印当前配置"""
    print("=" * 60)
    print("当前 API 配置")
    print("=" * 60)
    print(f"API Base: {config.OPENAI_API_BASE}")
    print(f"API Key: {'已设置' if config.OPENAI_API_KEY else '未设置'}")
    print(f"Model: {config.OPENAI_MODEL}")
    print(f"Extra Params: {config.get_extra_params()}")
    print(f"Extra Headers: {config.get_extra_headers()}")
    print(f"Response Path: {config.OPENAI_RESPONSE_PATH}")
    print()


async def test_chat_completion():
    """测试聊天补全 API"""
    print("=" * 60)
    print("测试聊天补全 API")
    print("=" * 60)
    print()

    async with AIClient() as client:
        messages = [
            {"role": "system", "content": "你是一个友好的助手。"},
            {"role": "user", "content": "你好！请用一句话介绍自己。"}
        ]

        try:
            print(f"发送请求到: {client.chat_completions_url}")
            print(f"模型: {client.model}")
            print()

            start_time = asyncio.get_event_loop().time()
            response = await client.chat_completion(
                messages=messages,
                temperature=0.7
            )
            elapsed_time = asyncio.get_event_loop().time() - start_time

            print("✅ 请求成功!")
            print(f"⏱️ 响应时间: {elapsed_time:.2f} 秒")
            print(f"🤖 模型: {response.model}")
            if response.usage:
                print(f"📝 Token 使用: {response.usage}")
            print()
            print("📝 AI 回复:")
            print("-" * 60)
            print(response.content)
            print("-" * 60)
            print()

            return True

        except Exception as e:
            print(f"❌ 请求失败: {e}")
            import traceback
            traceback.print_exc()
            return False


async def test_streaming():
    """测试流式输出"""
    print("=" * 60)
    print("测试流式输出 (Streaming)")
    print("=" * 60)
    print()

    async with AIClient() as client:
        messages = [
            {"role": "user", "content": "你好！"}
        ]

        try:
            print(f"发送流式请求到: {client.chat_completions_url}")
            print("接收到的内容: ", end="", flush=True)

            content_buffer = []
            # 注意: 流式输出需要实现 chat_completion_stream 方法
            # 这里简化处理，仅打印提示
            print("\n[流式输出测试需要额外实现 chat_completion_stream 方法]")
            print()

            return True

        except Exception as e:
            print(f"❌ 流式请求失败: {e}")
            import traceback
            traceback.print_exc()
            return False


def test_config_loading():
    """测试配置加载"""
    print("=" * 60)
    print("测试配置加载")
    print("=" * 60)
    print()

    print("原始配置值:")
    print(f"  OPENAI_EXTRA_PARAMS: {config.OPENAI_EXTRA_PARAMS}")
    print(f"  OPENAI_EXTRA_HEADERS: {config.OPENAI_EXTRA_HEADERS}")
    print()

    print("解析后的配置:")
    extra_params = config.get_extra_params()
    extra_headers = config.get_extra_headers()

    print(f"  Extra Params: {extra_params} (类型: {type(extra_params).__name__})")
    print(f"  Extra Headers: {extra_headers} (类型: {type(extra_headers).__name__})")
    print()

    # 验证类型
    assert isinstance(extra_params, dict), "extra_params 应该是字典类型"
    assert isinstance(extra_headers, dict), "extra_headers 应该是字典类型"

    print("✅ 配置加载测试通过!")
    print()


async def main():
    """主函数"""
    print()
    print("🚀 OpenAI 兼容 API 测试工具")
    print()

    # 显示配置
    print_config()

    # 测试配置加载
    test_config_loading()

    # 测试聊天补全
    success = await test_chat_completion()

    if success:
        # 询问是否测试流式
        print()
        # test_stream = input("是否测试流式输出? (y/n): ").strip().lower()
        # if test_stream == 'y':
        #     await test_streaming()

        print()
        print("=" * 60)
        print("✅ 所有测试完成!")
        print("=" * 60)
        print()
        print("现在可以启动机器人: python main.py")
    else:
        print()
        print("=" * 60)
        print("❌ 测试失败，请检查配置")
        print("=" * 60)
        print()
        print("常见问题:")
        print("1. 检查 API Key 是否正确")
        print("2. 检查 API Base URL 是否正确")
        print("3. 检查网络连接是否正常")
        print("4. 查看日志输出了解详细错误信息")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        print("\n\n测试已取消")
    except Exception as e:
        print(f"\n运行出错: {e}")
        import traceback
        traceback.print_exc()