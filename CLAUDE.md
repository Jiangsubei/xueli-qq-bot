# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## 项目概述

xueli 是一个轻量对话内核机器人框架，通过不同 adapter 接入 QQ (NapCat)、开放 API 等平台。核心设计原则是"轻量对话内核 + 薄多平台 adapter + 开放 API 接入层"。

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行单元测试
python -m unittest discover -s xueli/tests -t xueli

# 运行单个测试文件
python -m unittest xueli.tests.test_conversation_planner

# 启动 (Windows)
start.bat

# 启动 (Linux)
bash start.sh
```

## 核心架构

### 主消息处理链

```
MessageHandler
  └── PlanningWindowService (缓冲窗口调度)
        └── ConversationPlanner (reply/wait/ignore 决策 + PromptPlan)
              └── TimingGateService (时机判断 continue/wait/no_reply)
                    └── ConversationContextBuilder (构建上下文)
                          └── ReplyPipeline (PromptPlan + 上下文 → 最终回复)
                                └── ReplyGenerationService (AI 生成)
                                      └── MemoryFlowService (记忆写入)
```

### PromptPlan 契约

`PromptPlan` 是 planner 输出给 reply pipeline 的核心结构，包含：
- `action`: reply / wait / ignore
- `reply_reference`: planner 给 reply 的自然语言方向提示
- 各上下文层开关（memory、character_card 等）

### 三大提示词模板

- `xueli/prompts/zh-CN/planner.prompt` — 判断 reply/wait/ignore，输出 PromptPlan
- `xueli/prompts/zh-CN/timing_gate.prompt` — 判断 continue/wait/no_reply
- `xueli/prompts/zh-CN/reply.prompt` — 根据 PromptPlan + 上下文生成最终回复

模板通过 `PromptTemplateLoader` 加载，各服务补充动态 section。

### Adapter 模式

平台差异隔离在 `src/adapters/` 下：
- `napcat/adapter.py` — QQ/NapCat WebSocket 接入
- `api/adapter.py` — HTTP API 运行时接入

Core 不应被 QQ/NapCat 细节污染。

### 记忆系统

- `memory_manager.py` — 总管理器
- `person_fact_service.py` — 人物事实存储
- `chat_summary_service.py` — 会话摘要
- `conversation_recall_service.py` — 记忆检索召回
- `session_restore_service.py` — 重启后会话恢复
- `storage/` — SQLite/明文存储层

### 关键配置块 (config.toml)

| 配置块 | 作用 |
|--------|------|
| `adapter_connection` | 事件来源 (napcat/api) |
| `ai_service` | 主模型（回复生成） |
| `group_reply_decision` | 规划模型（独立可配） |
| `vision_service` | 图片理解模型 |
| `planning_window` | 缓冲窗口时长 |
| `bot_behavior` | 回复分段、延迟、上下文长度 |
| `memory` | 记忆开关与提取阈值 |

## 设计原则 (来自 AGENTS.md)

1. **Core 保持轻量** — QQ 明天消失，core 逻辑仍应有意义
2. **私聊/群聊共用主链** — 不要分裂两套业务逻辑
3. **PromptPlan 是契约** — planning 和 reply generation 之间的核心协议
4. **平台差异锁在 Adapter** — 不要把平台字段一路传进 core
5. **对话记录立即持久化** — 不依赖会话关闭；记忆按阈值提取，关闭时不强制提取

## 高风险改动

以下模块改动必须连带检查测试和相关调用点：
- `PromptPlan` / `ConversationPlanner` / `TimingGateService`
- `ReplyPipeline` / `ReplyPromptRenderer`
- `MessageHandler`
- `MemoryManager` / `MemoryFlowService`
- `BotRuntime` 主消息处理链

## 项目结构

```
xueli/
├── src/
│   ├── adapters/          # 平台适配器 (napcat, api)
│   ├── core/              # 运行时核心 (runtime, dispatcher, config)
│   ├── handlers/          # 消息处理链 (planner, timing, reply, context)
│   ├── memory/            # 记忆系统 (存储、检索、提取)
│   ├── services/          # AI 调用、图片服务
│   ├── emoji/             # 原生表情管理
│   └── webui/             # Django 控制台后端
├── prompts/zh-CN/         # 提示词模板文件
├── tests/                 # 单元测试
└── config/                # 配置文件
```
