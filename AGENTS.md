# AGENTS.md

## 常用命令

```bash
# 安装依赖
pip install -r requirements.txt

# 运行全部单元测试
python -m unittest discover -s xueli/tests -t xueli

# 运行单个测试文件
python -m unittest discover -s xueli/tests -t xueli -p test_conversation_planner.py

# 启动 (Windows)
start.bat
# 或 PowerShell
./start.ps1

# 启动 (Linux/macOS)
bash start.sh
```

- 没有配置 lint / typecheck / formatter 工具，不要运行 `ruff`、`mypy`、`pylint` 等。
- 测试基于 `unittest`，不要假设 `pytest` 存在。
- 测试通过 `-t xueli` 将 `xueli/` 设为 Python 路径根，测试内 import 使用 `src.*`（而非 `xueli.src.*`）。
- `xueli/config/config.toml` 是本地私有配置（已 gitignore），首次使用需从 `config.example.toml` 复制。

## 架构核心理念

`xueli` 目标是 `轻量对话内核 + 薄多平台 adapter + 开放 API 接入层`。

判断标准：**如果 QQ 明天消失，这段逻辑是否依然有意义、可被 HTTP / WebSocket / 其他平台复用？** 答案否定则不该进入 core。

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

群聊的 planner / timing gate 调用由 `ConversationPlanCoordinator` 编排，涉及 buffer window 合并、engagement 判断和调度并发控制。

### 核心契约：PromptPlan

`PromptPlan` 是 planner 输出给 reply pipeline 的核心结构，包含 `action` (reply/wait/ignore)、`reply_reference`（自然语言方向提示，仅为软指导）、各上下文层开关等。改 `PromptPlan` 是高风险操作，必须同步更新 planner、reply pipeline、prompt renderer 和测试。

### 三大提示词模板

- `xueli/prompts/zh-CN/planner.prompt` — 判断 reply/wait/ignore，输出 PromptPlan
- `xueli/prompts/zh-CN/timing_gate.prompt` — 判断 continue/wait/no_reply
- `xueli/prompts/zh-CN/reply.prompt` — 根据 PromptPlan + 上下文生成最终回复

模板通过 `PromptTemplateLoader` 加载，动态 section 由 `ReplyPromptRenderer`、`ReplyStylePolicy` 等在代码内注入。改 reply prompt 应优先改 section / renderer / style policy，不要退回大段字符串硬拼。

### Adapter 隔离

平台适配器在 `src/adapters/` 下：
- `napcat/adapter.py` — QQ/NapCat WebSocket
- `napcat/normalizer.py` — **OneBot → InboundEvent 协议归一化**（从 core 迁出，见下）
- `api/adapter.py` — HTTP API 运行时

Core 不应被 QQ / NapCat 细节污染，不要把平台字段一路传进 core。

**OneBot 归一化已从 core 迁出**：`normalize_onebot_message_event` / `attach_normalized_onebot_event` 现在位于 `adapters/napcat/normalizer.py`。`core/platform_normalizers.py` 仅保留平台无关的 helper 和向后兼容的 re-export。新 adapter 应实现自己的 `attach_inbound_event()` 并由 `dispatcher.py` 优先调用。

### 记忆系统

- `memory_manager.py` — 总管理器
- `memory_flow_service.py` — 记忆写入流程编排（回复后记忆动作统一在此层）
- `person_fact_service.py` / `chat_summary_service.py` / `conversation_recall_service.py` — 各记忆类型
- `session_restore_service.py` — 重启后会话恢复
- `storage/` — SQLite + Markdown 明文持久化
- `extraction/` — LLM 记忆提取（支持 emotional_tone 情绪标记）
- `retrieval/` — BM25 初排 + 向量语义联想 + 两阶段重排
- `internal/` — 内部工具（含 MemoryBackgroundCoordinator 离线消化）

两种模式：**按阈值触发**（`extract_every_n_turns`）和**关闭时不强制提取**。对话记录必须每轮立即持久化，不依赖会话关闭。

拟人化记忆特性：
- **动态遗忘**：检索命中的记忆自动回写 `last_recalled_at`/`mention_count`，用进废退
- **软遗忘**：归档记忆仍可被索引检索（分数打折 50%）
- **情绪标记**：提取时 LLM 标注 `emotional_tone`，检索时情绪加权匹配
- **重构输出**：普通记忆注入 prompt 时加转述指令
- **离线消化**：每 6 小时 LLM 扫描近期记忆归纳模式（`insight_type: digested`）
- **向量联想**：`VectorIndex`（字符 n-gram 余弦相似度）与 BM25 混合检索，零外部依赖

## 关键约束

1. 私聊和群聊共用一条 conversation 主链，不要分裂两套业务逻辑。
2. `ReplyPipeline` 定位是 prompt compiler，回复后副作用（记忆写入）应走 `MemoryFlowService`，不要塞回 pipeline。
3. 命名优先使用 `conversation_*`、`adapter_*`、`platform_*` 等中性命名，不要扩散 `group_*`、`napcat_*`。
4. 会话永不过期，重启后从历史存储恢复并保留原始时间信息。
5. 结构化分段发送是主路径（模型输出字符串数组）；正则分句仅作兜底。
6. 普通图片只做视觉理解，不入 emoji 仓库；原生表情只存 `face / mface` 引用。
7. WebUI 基于 Django 5.2，核心逻辑不要为 WebUI 方便写进 WebUI service。
8. `data/` 目录已 gitignore，是运行时产物，不提交。
9. `group_reply_decision` 配置未完整填写时，群聊退回规则路径（通常只在被 @ 时回复）。

## 高风险改动

以下模块改动必须连带检查测试和所有调用点：

- `PromptPlan` / `ConversationPlanner` / `TimingGateService`
- `ReplyPipeline` / `ReplyPromptRenderer`
- `MessageHandler`
- `MemoryManager` / `MemoryFlowService`
- `BotRuntime` 主消息处理链
- 三大 prompt 模板文件

## 改动流程

1. 先读相关入口代码，判断是 core / adapter / WebUI 逻辑。
2. 找已有契约和测试。
3. 沿现有主链路改，不要新开分支路径。
4. 改后运行受影响测试；如改了契约、配置方式、主链路，同步更新测试和文档。
5. 保持日志、类型、测试、实现四者一致。

## 编码习惯与已知陷阱

- **文件写入必须原子化**：所有持久化存储（Markdown/JSON）必须写 `.tmp` → `os.replace()`，禁止直接覆写目标文件。已修复的 4 处：`markdown_store`、`important_memory_store`、`person_fact_store`、`character_card_service`。
- **async 函数中 `except Exception` 必须在前面加 `except asyncio.CancelledError: raise`**：Python 3.9+ 中 `CancelledError` 继承自 `Exception`，不加守卫会破坏 asyncio 取消协议。
- **禁止用 `asyncio.CancelledError` 作业务流程控制**：应使用自定义异常（如 `StaleWindowError`），避免与任务取消混淆。
- **`Future.set_result()` 不要在持有 `asyncio.Lock` 时调用**：回调链路可能尝试获取同一把锁导致死锁。应收集 waiter → 锁外 resolve。
- **禁止在 `async` 上下文中使用同步阻塞 I/O**：`Path.read_text()` / `Path.write_text()` 应通过 `asyncio.to_thread()` 包裹，或使用 `aiofiles`。
