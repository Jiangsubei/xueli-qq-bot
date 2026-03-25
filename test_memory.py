"""测试记忆模块配置和初始化"""
import asyncio
import sys
import os

# 添加当前目录到路径
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from src.core.config import config

print("=" * 60)
print("测试配置读取")
print("=" * 60)

# 测试记忆模块配置
print(f"\n1. 直接访问 config.memory:")
try:
    memory_config = config.memory
    print(f"   成功: {memory_config}")
except Exception as e:
    print(f"   失败: {e}")

print(f"\n2. 使用 getattr 读取 MEMORY_ENABLED:")
memory_enabled = getattr(config, 'MEMORY_ENABLED', False)
print(f"   值: {memory_enabled}")
print(f"   类型: {type(memory_enabled)}")
print(f"   布尔判断: {bool(memory_enabled)}")

print(f"\n3. 读取其他记忆配置:")
for key in ['MEMORY_STORAGE_PATH', 'MEMORY_BM25_TOP_K', 'MEMORY_AUTO_EXTRACT']:
    try:
        value = getattr(config, key, None)
        print(f"   {key}: {value}")
    except Exception as e:
        print(f"   {key}: 错误 - {e}")

print("\n" + "=" * 60)
print("测试记忆模块初始化")
print("=" * 60)

async def test_memory():
    if not memory_enabled:
        print("\n记忆模块已禁用，跳过初始化测试")
        return

    try:
        from memory import MemoryManager, MemoryManagerConfig, RetrievalConfig

        print("\n1. 创建 MemoryManager 实例...")

        # 简单的 LLM 回调（仅用于测试）
        async def test_llm_callback(system_prompt, messages):
            return "测试记忆提取结果"

        mm_config = MemoryManagerConfig(
            storage_base_path=getattr(config, 'MEMORY_STORAGE_PATH', 'memories'),
            retrieval_config=RetrievalConfig(
                bm25_top_k=100,
                rerank_enabled=False,
                rerank_top_k=20
            ),
            auto_extract_memory=False,  # 测试中禁用自动提取
            auto_build_index=True
        )

        manager = MemoryManager(
            llm_callback=test_llm_callback,
            config=mm_config
        )

        print("2. 初始化 MemoryManager...")
        await manager.initialize()

        print("3. 测试添加记忆...")
        from memory import MarkdownMemoryStore
        test_mem = await manager.add_memory(
            content="这是一条测试记忆",
            user_id="test_user",
            tags=["test"]
        )

        if test_mem:
            print(f"   成功添加记忆: {test_mem.id}")
        else:
            print("   添加记忆返回 None（可能已存在）")

        # 检查目录是否创建
        import os
        if os.path.exists('memories'):
            print(f"   memories 目录已创建")
            if os.path.exists('memories/users'):
                print(f"   memories/users 目录已创建")
                files = os.listdir('memories/users')
                print(f"   用户文件: {files}")
        else:
            print("   WARNING: memories 目录未创建！")

        print("\n4. 关闭 MemoryManager...")
        await manager.close()
        print("   完成")

    except Exception as e:
        print(f"\n错误: {e}")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(test_memory())
    print("\n" + "=" * 60)
    print("测试完成")
    print("=" * 60)
