# QQ 机器人项目摘要

## 当前项目定位

这是一个基于 NapCat 和 OpenAI 兼容接口的 QQ 机器人项目，当前以仓库根目录的 `config.toml` 作为主配置入口。

项目当前支持：

- 私聊与群聊回复
- 群聊规划与复读触发
- 图片理解
- 长期记忆提取、检索与写入
- 本地 WebUI 控制台

## 当前配置方式

项目已经切换到 **`config.toml` 配置模式**。

以下旧说明不再作为当前主流程依据：

- `.env` 启动方式
- `.env.example` 作为主配置入口
- `OPENAI_API_BASE` / `OPENAI_API_KEY` / `OPENAI_MODEL` 作为唯一配置来源

如果文档或旧脚本里仍提到 `.env`，请以 `config.toml` 和 `src/core/config.py` 的实际实现为准。

## 当前关键配置块

`config.toml` 主要包含这些配置区块：

- `napcat`
- `ai_service`
- `vision_service`
- `bot_behavior`
- `assistant_profile`
- `group_reply`
- `group_reply_decision`
- `personality`
- `dialogue_style`
- `behavior`
- `memory_rerank`
- `memory`

其中：

- `ai_service` 是主模型配置
- `vision_service` 是图片理解模型配置
- `group_reply_decision` 是群聊判断模型配置，未单独填写时会回退到 `ai_service`
- `memory.extraction_*` 是记忆提取模型配置，未单独填写时会回退到 `ai_service`
- `memory_rerank` 是记忆重排配置，未完整配置时视为未启用

## 当前项目结构

```text
main.py                     启动入口
config.toml                 主配置文件
src/core/                   核心运行与配置装配
src/handlers/               消息处理与回复流程
src/services/               AI / Vision / Image 客户端
src/emoji/                  表情包采集、分类、追发
src/memory/                 记忆提取、检索、存储
src/webui/                  本地 WebUI
tests/                      自动化测试
```

## 当前运行主链路

1. `main.py` 启动程序
2. `src/core/config.py` 读取 `config.toml`
3. `src/core/bootstrap.py` 组装各个运行组件
4. `src/core/bot.py` 连接 NapCat 并处理消息
5. `src/handlers/` 调用 AI、视觉、记忆与群聊规划逻辑
6. `src/webui/` 提供运行状态控制台

## 当前重点模块

### 核心模块
- `src/core/config.py`
- `src/core/bootstrap.py`
- `src/core/runtime_supervisor.py`
- `src/core/bot.py`

### 消息与回复模块
- `src/handlers/message_handler.py`
- `src/handlers/reply_pipeline.py`
- `src/handlers/group_reply_planner.py`
- `src/handlers/group_plan_coordinator.py`

### 记忆模块
- `src/memory/memory_manager.py`
- `src/memory/extraction/memory_extractor.py`
- `src/memory/internal/background_coordinator.py`
- `src/memory/retrieval/two_stage_retriever.py`

## 当前已确认的行为

- `memory.extract_every_n_turns` 已接入实际提取链路
- `memory.extraction_api_*` 为空时，会回退到 `ai_service`
- `group_reply_decision` 未完整配置时，会回退到 `ai_service`
- WebUI 启动失败不会阻塞机器人主流程

## 当前建议关注点

- 文档统一以 `config.toml` 为准
- 继续减少过宽的异常捕获
- 为记忆提取、关闭 flush、群聊规划补更多回归测试
