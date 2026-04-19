# xueli

`xueli` 当前的方向是：

`轻量对话内核 + 薄多平台 adapter + 开放 API 接入层`

它不再只被看作一个 QQ 机器人项目。当前仓库里已经保留了 QQ / NapCat 路径，同时也开始具备标准化入站事件、标准化出站动作，以及开放 API 接入能力。

## 当前能力

- 私聊与群聊回复
- 统一会话规划、复读触发与图片理解
- 结构化主动陪伴与 `engagement_mode` 驱动的回复编排
- 长期记忆提取、检索与写入
- 会话摘要恢复、旧对话精准召回、人物事实分层
- PromptPlan 驱动的动态 prompt layer 编译
- 私聊短窗口 batching 与 `reply / wait / ignore`
- 本地 WebUI 控制台
- NapCat adapter
- API adapter
- 标准 `InboundEvent` / `ReplyAction` / `ImageAction` 数据流
- 独立轻量 API runtime 第一版

## 当前架构

项目现在大致分成四层：

1. 运行入口
2. 平台无关核心
3. 薄 adapter 层
4. WebUI 与数据层

关键目录：

```text
main.py                     启动入口
config.toml                 主配置文件
src/core/                   平台无关核心运行逻辑
src/adapters/               平台 adapter
src/handlers/               消息处理链
src/services/               AI / Vision / 图片等服务
src/emoji/                  表情追发与分类
src/memory/                 记忆系统
src/webui/                  本地 WebUI
tests/                      自动化测试
```

## 当前主链路

### 入站

1. NapCat 或 API payload 进入 adapter
2. adapter 归一化为标准 `InboundEvent`
3. `BotRuntime.ingest_adapter_payload(...)` / `ingest_inbound_event(...)`
4. `EventDispatcher` 分发
5. `MessageHandler` / planner / reply pipeline 处理

### 统一规划与回复编译

当前的消息处理链不再区分“群聊有 planner，私聊直接回复”这种旧路径，而是统一变成：

1. `MessageHandler` 先整理消息事实、窗口上下文、图片信息、时间跨度和 planning signals
2. `ConversationPlanner` 先决定当前动作是 `reply`、`wait` 还是 `ignore`
3. 如果动作是 `reply`，planner 继续产出 `PromptPlan`
4. `ReplyPipeline` 按 `PromptPlan` 动态编译 prompt layer，再调用主回复模型
5. memory retrieval 也按 `PromptPlan` 按需加载对应层，而不是固定把所有记忆都塞进 prompt

这里的关键边界是：

- 代码负责收集事实信号与执行动作
- planner 负责动作判断和 prompt layer 规划
- `new_session_prompt` 已移除，改由 `temporal_context` 提供时间连续性事实

### 出站

1. 核心链路决定回复动作
2. 生成 `ReplyAction` / `ImageAction`
3. 根据 session 的 platform 选择对应 adapter
4. adapter 转成平台 payload 发出

## 运行要求

至少准备：

- Python 3.10+
- 一个可用的 OpenAI 兼容接口

如果你要走 QQ / OneBot 路径，还需要：

- 一个可工作的 NapCat

## 配置方式

当前主配置入口是仓库根目录的 `config.toml`。

连接配置已经从旧的 `napcat` 命名迁到更中性的 `adapter_connection`。

推荐写法：

```toml
[adapter_connection]
adapter = "napcat"
platform = "qq"
ws_url = "ws://127.0.0.1:8095"
http_url = "http://127.0.0.1:6700"

[ai_service]
api_base = "https://your-openai-compatible-endpoint/v1"
api_key = "sk-xxxx"
model = "your-model"
response_path = "choices.0.message.content"

[ai_service.extra_params]

[ai_service.extra_headers]
```

说明：

- 新配置优先读取 `[adapter_connection]`
- 旧的 `[napcat]` 仍然兼容读取
- WebUI 保存网络设置时会自动写回 `[adapter_connection]`

关键配置块：

- `adapter_connection`：事件 adapter 的连接地址
- `ai_service`：主模型
- `vision_service`：识图模型
- `group_reply`：群聊侧的节流、兴趣回复与复读策略
- `group_reply_decision`：统一会话规划模型
- `bot_behavior.private_batch_window_seconds`：私聊短窗口合批时长
- `memory`：长期记忆
- `memory_rerank`：记忆重排

## 启动方式

安装依赖：

Windows:

```bash
python -m venv venv
venv\Scripts\activate
python -m pip install -r requirements.txt
```

Linux / macOS:

```bash
python3 -m venv venv
source venv/bin/activate
python3 -m pip install -r requirements.txt
```

启动：

```bash
python main.py
```

默认会启动：

- `BotRuntimeSupervisor`
- `BotRuntime`
- 本地 WebUI runtime server

如果启用了开放 API runtime，也会额外启动：

- 独立 HTTP ingress server

## 独立 API runtime

第一版开放 API 入口已经存在，默认关闭。

可用环境变量：

- `API_RUNTIME_ENABLED=true`
- `API_RUNTIME_HOST=127.0.0.1`
- `API_RUNTIME_PORT=8765`
- `API_RUNTIME_TIMEOUT=10`

当前接口：

- `GET /health`
- `POST /events`

`POST /events` 的 payload 会先走 `ApiAdapter.normalize_inbound_payload(...)`，再进入现有 bot 核心链路。

## 当前关键模块

### 核心

- `src/core/runtime.py`
- `src/core/bootstrap.py`
- `src/core/runtime_supervisor.py`
- `src/core/dispatcher.py`
- `src/core/platform_models.py`
- `src/core/platform_normalizers.py`
- `src/core/platform_bridge.py`

### adapter

- `src/adapters/base.py`
- `src/adapters/registry.py`
- `src/adapters/napcat/adapter.py`
- `src/adapters/napcat/connection.py`
- `src/adapters/api/adapter.py`
- `src/adapters/api/runtime.py`

### 消息处理链

- `src/handlers/message_handler.py`
- `src/handlers/reply_pipeline.py`
- `src/handlers/conversation_planner.py`
- `src/handlers/conversation_engagement.py`
- `src/handlers/conversation_plan_coordinator.py`
- `src/handlers/prompt_planner.py`
- `src/handlers/temporal_context.py`

### 记忆系统

- `src/memory/memory_manager.py`
- `src/memory/chat_summary_service.py`
- `src/memory/session_restore_service.py`
- `src/memory/conversation_recall_service.py`
- `src/memory/person_fact_service.py`
- `src/memory/storage/conversation_store.py`
- `src/memory/storage/person_fact_store.py`

当前记忆链路已经不是单一“检索几条长期记忆”模式，而是分成几层：

- `person_fact_context`：用户稳定事实、偏好、边界、计划等
- `session_restore_context`：同一 dialogue 最近一轮会话的摘要恢复
- `precise_recall_context`：围绕当前 query 的第一次提及 / 最近一次提及定位
- `persistent_memory_context`：重要但不适合塞进人物事实层的长期关键信息
- `dynamic_memory_context`：与当前消息动态相关的普通记忆

其中：

- 会话关闭后会自动生成摘要并持久化到 conversation metadata
- 重要记忆会同步沉淀为结构化人物事实
- 普通记忆遗忘不再只看单一半衰期，还会结合提及次数、观察锚点和近期强化时间
- 哪些层启用、启用多强，现在由 `PromptPlan` 控制
- retrieval 侧会按 layer policy 和 intensity 决定是否查询 `session_restore / precise_recall / dynamic_memory / recent_history`

## 时间与私聊策略

当前不再使用 `new_session_prompt` 这类静态结论，而是统一使用 `temporal_context`：

- 最近消息时间分层
- 当前会话时间分层
- 上一轮会话时间分层
- continuity hint

这些都是 planner 的事实输入，不是代码替模型做出的结论。

私聊路径也已经改成统一 planner：

- 私聊不再默认必回复
- 支持 `reply / wait / ignore`
- 支持短窗口 batching，把连续碎片输入合并后再规划
- 显式“等一下 / 我补充”类信号会直接触发强 hold
- 群聊与私聊都会把陪伴信号整理成 `planning_signals`，由 `PromptPlan.engagement_mode` 决定更适合轻关怀、延续话题还是轻量存在感

## 模型调用路由

`src/core/model_invocation_router.py` 现在统一管理不同用途的模型调用队列与超时策略：

- `GROUP_PLAN`
- `REPLY_GENERATION`
- `EMOJI_REPLY_DECISION`
- `VISION_ANALYSIS`
- `VISION_STICKER_EMOTION`
- `MEMORY_EXTRACTION`
- `MEMORY_RERANK`

当前策略是：

- 快决策任务使用更短的 purpose timeout
- 回复生成、视觉分析、记忆提取继承主超时预算
- 记忆重排可按自己的较短 timeout 显式覆盖

## 当前状态说明

仓库已经完成这些方向上的改造：

- 去掉了核心里的默认 QQ-only 回复路径
- 标准化了入站事件和出站动作
- 让 API adapter 能进入现有处理链
- 让回复动作能按 session.platform 选择正确 adapter
- 把运行类命名切到中性的 `BotRuntime`
- 把 NapCat transport 移到了 adapter 边界
- 把私聊和群聊统一到同一条 conversation planner 主链
- 把 prompt 控制权从静态拼接推进到 planner 产出的 `PromptPlan`
- 把主动陪伴从粗粒度 proactive 标记推进到结构化 `engagement_mode`
- 移除了 `new_session_prompt`，改用 `temporal_context`
- 把记忆上下文拆成了人物事实 / 会话恢复 / 精准召回 / 动态记忆几层
- 给普通记忆补上了更稳的多因子遗忘逻辑
- 给模型调用路由补上了 per-purpose timeout policy
- 给 runtime 到 WebUI serializer 的高层闭环补上了集成测试

仍然值得继续做的主要是：

- 继续拆分 `src/webui/console/services.py`
- 继续收紧 `MessageEvent` 和 `InboundEvent` 的边界
- 在完整依赖环境下补跑更大范围测试
- 如果未来接更多平台，再继续扩大 adapter 覆盖面

## 测试

项目已经有一组围绕这条多平台主线的 focused tests。

常用运行方式：

```bash
venv\Scripts\python.exe -m unittest tests.test_platform_models tests.test_platform_normalizers tests.test_napcat_adapter tests.test_api_adapter tests.test_api_runtime tests.test_api_ingress_bridge tests.test_bot_api_ingress tests.test_bot_adapter_send_path tests.test_dispatcher_inbound_wiring tests.test_message_handler_inbound_event tests.test_downstream_inbound_helpers tests.test_conversation_planner_context_preference tests.test_private_planning tests.test_temporal_context tests.test_prompt_planner tests.test_conversation_planner tests.test_conversation_planner_prompt_signals tests.test_model_invocation_router tests.test_reply_pipeline_prompt_plan tests.test_reply_pipeline_memory_layer_policy tests.test_config_adapter_connection tests.test_console_network_settings tests.test_runtime_supervisor tests.test_session_restore_service tests.test_memory_session_restore_context tests.test_reply_pipeline_session_restore tests.test_conversation_recall_service tests.test_person_fact_service tests.test_memory_forgetting tests.test_runtime_conversation_flow_integration
```

## 额外说明

仓库里如果还有旧文档把它描述成“QQ Bot”或把 `napcat` 视为唯一入口，请以当前 `src/core/`、`src/adapters/` 和本 README 为准。
