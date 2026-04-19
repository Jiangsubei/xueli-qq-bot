# 项目摘要

## 当前定位

`xueli` 当前不是单一平台机器人仓库，而是一个正在演进中的轻量多平台对话内核。

当前目标是：

- 保持核心轻量
- 把平台细节压到 adapter 边界
- 让开放 API 也作为一种 adapter 接入
- 逐步让核心逻辑依赖标准事件和标准动作，而不是默认依赖 QQ / OneBot

## 当前已经落地的关键改造

### 标准模型与归一化

- `src/core/platform_models.py`
- `src/core/platform_normalizers.py`
- `src/core/platform_bridge.py`

已具备：

- `InboundEvent`
- `ReplyAction`
- `ImageAction`
- `SessionRef`
- OneBot/NapCat -> 标准事件归一化
- 标准事件 -> 兼容 `MessageEvent` 桥接

### adapter 层

- `src/adapters/base.py`
- `src/adapters/registry.py`
- `src/adapters/napcat/adapter.py`
- `src/adapters/napcat/connection.py`
- `src/adapters/api/adapter.py`
- `src/adapters/api/runtime.py`

当前已有两类 adapter：

- `napcat`
- `api` / `openapi`

### runtime 与处理链

- `src/core/runtime.py`
- `src/core/runtime_supervisor.py`
- `src/core/dispatcher.py`
- `src/handlers/message_handler.py`
- `src/handlers/reply_pipeline.py`
- `src/handlers/conversation_planner.py`
- `src/handlers/conversation_engagement.py`
- `src/handlers/prompt_planner.py`
- `src/handlers/temporal_context.py`
- `src/handlers/conversation_plan_coordinator.py`

当前已实现：

- `BotRuntime.ingest_adapter_payload(...)`
- `BotRuntime.ingest_inbound_event(...)`
- adapter 驱动的入站 attach hook
- 回复动作按 `session.platform` 选择对应 adapter
- mention / quote 优先读取 attached `InboundEvent`
- session / execution key 平台限定化
- 私聊和群聊统一走 `ConversationPlanner`
- planner 先决定 `reply / wait / ignore`，再产出 `PromptPlan`
- `PromptPlan` 已增加 `engagement_mode`，用于表达轻关怀、延续话题和轻量存在感
- `ReplyPipeline` 按 `PromptPlan` 动态编译 prompt layer
- `new_session_prompt` 已被 `temporal_context` 替代

## 当前配置方式

主配置入口仍然是根目录 `config.toml`。

连接配置现在推荐使用：

- `[adapter_connection]`

兼容读取：

- `[napcat]`

保存网络设置时会自动迁移到：

- `[adapter_connection]`

## 当前运行主链路

1. `main.py` 启动程序
2. `src/core/config.py` 读取配置
3. `src/core/bootstrap.py` 组装默认 runtime 组件
4. `src/core/runtime.py` 管理 bot 运行时
5. `src/adapters/napcat` 或 `src/adapters/api` 提供入站/出站 adapter 能力
6. `src/handlers/` 执行消息处理、统一规划、prompt 编译、回复、视觉、记忆逻辑
7. `src/webui/` 提供本地控制台

更具体的顺序现在是：

1. `MessageHandler` 收集 recent window、图片理解结果、temporal context、planning signals
2. `ConversationPlanner` 判断动作并返回 `PromptPlan`
3. `ReplyPipeline` 按 layer policy 编译回复 prompt
4. `MemoryManager` / retrieval coordinator 按 `PromptPlan` 决定启用哪些 memory layer 和 intensity
5. 回复模型生成最终回答，后续可再触发表情跟进

## 当前记忆系统状态

### 新增的分层能力

- `src/memory/chat_summary_service.py`
- `src/memory/session_restore_service.py`
- `src/memory/conversation_recall_service.py`
- `src/memory/person_fact_service.py`
- `src/memory/storage/person_fact_store.py`

当前记忆上下文已经拆成：

- 人物事实层：稳定事实、偏好、边界、计划
- 会话恢复层：上一轮同 dialogue 会话摘要
- 精准召回层：围绕当前 query 的首次提及和最近提及定位
- 持续关键信息层：重要但不适合归到人物事实里的长期信息
- 动态普通记忆层：和当前消息临时相关的普通记忆

当前变化点是：

- 这些层不再默认全部启用
- 是否启用、启用多强由 `PromptPlan` 决定
- retrieval 已支持 `include_sections` 和 `section_intensity`
- `recent_history` 也已进入可规划层，而不是永远固定注入

## 当前 planner / prompt 架构状态

已经明确的边界是：

- 代码负责提供事实信号，不替模型下结论
- planner prompt 尽量中性，只描述观察到的时间跨度、图片、recent context 和 planning signals
- 模型先做 action planning，再做 prompt planning
- `ReplyPipeline` 更像 prompt compiler，而不是静态 prompt 拼接器

新增的私聊策略：

- 私聊不再默认必回复
- 支持 `reply / wait / ignore`
- 增加短窗口 batching，避免用户连发时被逐条抢答

新增的主动陪伴策略：

- `conversation_engagement.py` 负责从消息文本和最近上下文里提取陪伴信号
- `planning_signals` 会进入 planner 和 `PromptPlanner`
- `ReplyPipeline` 会消费 `PromptPlan.engagement_mode`，而不是只依赖一个粗粒度 proactive 标记

新增的时间策略：

- 统一使用 `temporal_context`
- 区分 private / group 的时间分桶阈值
- 用 continuity hint 表达连续性事实，而不是代码直接宣告“这是新会话”

## 当前模型调用路由状态

- `src/core/model_invocation_router.py`

当前模型调用已经按 purpose 进入独立 FIFO worker，并补上了标准化 timeout policy：

- `GROUP_PLAN`
- `REPLY_GENERATION`
- `EMOJI_REPLY_DECISION`
- `VISION_ANALYSIS`
- `VISION_STICKER_EMOTION`
- `MEMORY_EXTRACTION`
- `MEMORY_RERANK`

其中快决策类任务默认更短，回复生成和主链路任务继承主超时预算，记忆重排支持显式较短超时覆盖。

### 当前写入与遗忘逻辑

- 会话关闭后自动持久化摘要到 conversation metadata
- 重要记忆会同步整理出结构化人物事实
- 普通记忆遗忘采用多因子分数，而不是只靠单一半衰期
- 重要记忆和人物事实不进入普通遗忘路径

## 独立 API runtime

仓库里已经有一版独立轻量 API runtime：

- `src/adapters/api/runtime.py`

特点：

- 不挂在 Django WebUI 里
- 通过 runtime registry 找到当前 bot
- 请求进入后复用现有 bot 链路

当前接口：

- `GET /health`
- `POST /events`

## 当前重点模块

### 核心运行
- `src/core/config.py`
- `src/core/bootstrap.py`
- `src/core/runtime.py`
- `src/core/runtime_supervisor.py`

### 标准化与调度
- `src/core/platform_models.py`
- `src/core/platform_normalizers.py`
- `src/core/platform_bridge.py`
- `src/core/dispatcher.py`

### adapter
- `src/adapters/napcat/adapter.py`
- `src/adapters/napcat/connection.py`
- `src/adapters/api/adapter.py`
- `src/adapters/api/runtime.py`

### 处理链
- `src/handlers/message_handler.py`
- `src/handlers/reply_pipeline.py`
- `src/handlers/conversation_planner.py`
- `src/handlers/conversation_engagement.py`
- `src/handlers/prompt_planner.py`
- `src/handlers/temporal_context.py`
- `src/handlers/conversation_plan_coordinator.py`

### 记忆
- `src/memory/memory_manager.py`
- `src/memory/internal/retrieval_coordinator.py`
- `src/memory/internal/background_coordinator.py`
- `src/memory/storage/conversation_store.py`
- `src/memory/storage/markdown_store.py`
- `src/memory/storage/person_fact_store.py`

## 当前确认状态

- 旧 QQ / NapCat 路径仍可工作
- API adapter 已能进入同一条核心消息链
- 回复已不再默认回退到 QQ session
- WebUI 表层命名已开始去 QQ 默认化
- runtime / adapter / config 迁移已有 focused tests 覆盖
- unified conversation planner 已介入私聊和群聊
- memory prompt 已显式分成事实 / 会话恢复 / 精准召回 / 动态记忆几层
- prompt layer 已开始由 `PromptPlan` 驱动
- 主动陪伴已经进入结构化 `engagement_mode` 阶段
- `temporal_context` 与 private batching 已有 focused tests 覆盖
- 会话摘要、人物事实、精准召回、多因子遗忘已有 focused tests 覆盖
- 已新增 `runtime -> planner -> memory -> reply -> WebUI serializer` 高层闭环测试

## 当前测试面

当前 focused tests 已覆盖：

- platform models / normalizers
- NapCat adapter
- API adapter
- API runtime
- API ingress bridge
- bot ingress / send path
- dispatcher inbound wiring
- message handler inbound preference
- downstream helper preference
- planner context preference
- private planning / batching
- temporal context
- prompt planner
- planner companionship signals
- model invocation router timeout behavior
- runtime conversation flow integration
- adapter connection config migration
- network settings migration
- runtime supervisor lifecycle
- session restore / memory prompt layering
- conversation recall
- person fact sync
- multi-factor memory forgetting

## 后续更适合继续做的事情

- WebUI 展示 API runtime 状态
- 文档继续清理历史遗留表述
- 继续拆分 WebUI `services.py` 大文件
- 在完整依赖环境下补跑更大范围测试
- 如接入更多平台，继续沿现有 adapter 边界扩展
- 如需继续做 memory 主线，下一步更适合落在人物事实抽取质量和 recall ranking 精细化
