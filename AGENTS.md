# AGENTS.md

## 项目核心理念

`xueli` 是一个轻量项目。

目标不是做成“大而全机器人平台”，而是逐步演进为：

`轻量对话内核 + 薄多平台 adapter + 开放 API 接入层`

做任何改动前，先判断这件事是否符合下面几个原则：

1. 核心运行逻辑要尽量小、清楚、可读。
2. 私聊和群聊尽量走统一 conversation 主链，而不是分裂出两套业务逻辑。
3. `PromptPlan` 是 planning 和 reply generation 之间的核心契约。
4. adapter 负责平台差异，core 不应该被 QQ / NapCat 细节反向污染。
5. 优先做可复用、平台无关的抽象，而不是平台特供逻辑。

一句话判断标准：

如果 QQ 明天消失，这段逻辑是否依然有意义、可被 HTTP / WebSocket / 其他平台复用？

如果答案是否定的，它大概率不该进入 core。

## 代码规范

### 命名与结构

1. 优先使用中性命名，如 `conversation_*`、`adapter_*`、`platform_*`。
2. 不要继续扩散 `group_*`、`napcat_*` 这类只适合旧阶段的业务命名，除非它确实属于兼容边界。
3. 新能力优先通过 service / builder / coordinator 拆分，不要把 `MessageHandler` 和 `ReplyPipeline` 重新堆胖。
4. `ReplyPipeline` 更像 prompt compiler；不要再把回复后副作用塞回去。
5. 回复后的记忆动作应优先放在 `MemoryFlowService` 一类流程层，而不是 prompt/LLM 组织层。

### 设计原则

1. 优先小步演进，不要无必要大重写。
2. 优先兼容现有运行路径，除非当前任务明确要求升级接口。
3. 抽象必须服务当前代码，而不是为了“未来可能会用到”提前过度设计。
4. 公共契约变更时，要同步更新测试、日志和相关序列化使用点。
5. 更看重清晰边界和集成稳定性，而不是一次性塞很多新概念。

### 测试与验证

1. 改动核心链路时，优先补或改集成测试。
2. 如果改动影响 `PromptPlan`、planner、reply pipeline、memory flow、runtime，必须验证对应测试。
3. 当前仓库默认可依赖 `unittest`；如果环境里没有 `pytest`，不要假设它存在。
4. 如果某个测试只是在适配旧实现，且已经不符合新主链路，应同步迁移测试，而不是只为了兼容旧断言保留坏设计。

## 如何改动

### 改动前

1. 先读相关入口代码，确认改动落点。
2. 先判断这是 core 逻辑、adapter 逻辑，还是 WebUI / 配置逻辑。
3. 先找已有契约和测试，不要直接跳进去加分支。

### 改动时

1. 优先沿现有主链路改：`MessageHandler -> ConversationPlanner -> TimingGateService -> ConversationContextBuilder -> ReplyPipeline/ReplyPromptRenderer -> ReplyGenerationService -> MemoryFlowService`
2. 如果改 planner，就同时考虑 `PromptPlan`、timing、prompt renderer、测试是否要一起变。
3. 如果改 reply prompt，就优先改 section / renderer / style policy，不要退回大段字符串硬拼。
4. 如果改 memory，就优先区分“检索能力”和“流程编排”，不要把写回逻辑散落回 handler/pipeline。
5. 如果改 adapter，尽量把影响锁在 adapter 边界，不要把平台字段一路传进 core。

### 改动后

1. 运行受影响测试。
2. 如改了主链路、配置方式、核心约定，同步更新文档。
3. 保持日志、类型、测试、实现四者一致，不要只改其中一部分。

## 注意事项

### 应该做什么

1. 应该优先保持 runtime core 可读。
2. 应该优先抽服务、收边界、补测试。
3. 应该优先让 private/group 共用一套 conversation 逻辑。
4. 应该优先把平台差异留在 adapter。
5. 应该优先做“当前就能提升稳定性和可维护性”的改动。

### 不该做什么

1. 不要引入重型 plugin runtime。
2. 不要把仓库拆成很多进程或很多 repo，除非任务明确要求。
3. 不要为了 WebUI 方便，把核心逻辑写进 WebUI service。
4. 不要把平台特定业务逻辑直接塞进 core。
5. 不要为了兼容旧结构，继续扩散已经准备收敛掉的命名和边界。
6. 不要在没有测试兜底的情况下，静默重写核心链路。

### 高风险改动提醒

下面这些改动默认视为高风险，必须连带检查测试和相关调用点：

1. `PromptPlan`
2. `ConversationPlanner`
3. `TimingGateService`
4. `ReplyPipeline` / `ReplyPromptRenderer`
5. `MessageHandler`
6. `MemoryManager` / `MemoryFlowService`
7. `BotRuntime` 主消息处理链

### 当前工作倾向

当前更鼓励的方向：

1. 命名收敛
2. service extraction
3. integration coverage
4. platform-neutral models
5. 保持主链路清晰

当前不鼓励的方向：

1. 广铺新概念但没有测试
2. 只为了“架构好看”做大规模重写
3. 增加新的平台耦合
4. 增加只在单一聊天平台成立的业务规则
