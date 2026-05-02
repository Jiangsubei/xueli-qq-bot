# AGENTS.md

## 常用命令

```bash
# 安装依赖（推荐使用 uv）
uv pip install -r requirements.txt

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

---

## 修改项目原则

### 1. 测试先行
每次代码改动后必须运行全量测试（`python -m unittest discover -s xueli/tests -t xueli`），测试不通过不提交。
- 已发现并修复跨平台路径问题（`tempfile` 替代硬编码路径）
- 已修复 asyncio 时序不稳定问题（调整超时和等待逻辑）

### 2. 契约优先
核心数据结构（`PromptPlan`、`ReplyResult`、`MessageContext` 等）的修改必须同步更新所有调用方、测试和文档。
- 已修复 label 常量分散问题，新增 `label_constants.py` 统一管理

### 3. asyncio 正确性
`async` 函数中 `except Exception` 前必须先 `except asyncio.CancelledError: raise`，防止取消协议被吞。
- 约45处核心路径已修复
- 禁止用 `asyncio.CancelledError` 作业务流程控制，应使用自定义异常（如 `StaleWindowError`）

### 4. 原子化存储
所有持久化文件写入必须先写 `.tmp` 再 `os.replace()`，禁止直接覆盖目标文件。
- 已修复 4 处：`markdown_store`、`important_memory_store`、`person_fact_store`、`character_card_service`

### 5. 轻量外部依赖
优先使用 Python 标准库（如 `random` 而非 `numpy`、`jieba` 分词而非外部向量服务），保持轻量化。

### 6. 配置即文档
`config.example.toml` 包含完整注释，所有字段均有说明；`config.toml` 为实际运行配置。配置项与代码默认值严格对齐。
- 已新增 `[planning_window]`、`[memory_dispute]`、`[character_growth]` 三个 section
- 已补充分段发送6个字段（`sentence_split_enabled`、`segmented_reply_enabled`、`max_segments` 等）
- 已新增记忆检索融合权重 `vector_weight`（向量与 BM25 混合检索的融合比例）

### 7. README 与代码同步
代码审查后发现 README 描述与实现完全吻合（结构化分段发送、随机延迟均已实现），无需修改 README。
- `config.example.toml` 是用户了解功能的主要窗口，注释即文档

### 8. 平台解耦
核心逻辑不依赖平台细节，平台字段通过 `normalizer.py` 归一化后进入 core。
- `platform_normalizers.py` 仅保留平台无关的 helper 和向后兼容的 re-export
- 新 adapter 应实现自己的 `attach_inbound_event()` 并由 `dispatcher.py` 优先调用

---

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

---

## 关键约束

1. 私聊和群聊共用一条 conversation 主链，不要分裂两套业务逻辑。
2. `ReplyPipeline` 定位是 prompt compiler，回复后副作用（记忆写入）应走 `MemoryFlowService`，不要塞回 pipeline。
3. 命名优先使用 `conversation_*`、`adapter_*`、`platform_*` 等中性命名，不要扩散 `group_*`、`napcat_*`。
4. 会话永不过期，重启后从历史存储恢复并保留原始时间信息。
5. 结构化分段发送是主路径（模型输出 JSON 数组格式）；正则分句仅作兜底。
6. 普通图片只做视觉理解，不入 emoji 仓库；原生表情只存 `face / mface` 引用。
7. WebUI 基于 Django 5.2，核心逻辑不要为 WebUI 方便写进 WebUI service。
8. `data/` 目录已 gitignore，是运行时产物，不提交。
9. `group_reply_decision` 配置未完整填写时，群聊退回规则路径（通常只在被 @ 时回复）。

---

## 高风险改动

以下模块改动必须连带检查测试和所有调用点：

- `PromptPlan` / `ConversationPlanner` / `TimingGateService`
- `ReplyPipeline` / `ReplyPromptRenderer`
- `MessageHandler`
- `MemoryManager` / `MemoryFlowService`
- `BotRuntime` 主消息处理链
- 三大 prompt 模板文件

---

## 改动流程

1. 先读相关入口代码，判断是 core / adapter / WebUI 逻辑。
2. 找已有契约和测试。
3. 沿现有主链路改，不要新开分支路径。
4. 改后运行受影响测试；如改了契约、配置方式、主链路，同步更新测试和文档。
5. 保持日志、类型、测试、实现四者一致。

---

## 依赖协议约束

项目采用 **MIT 许可证**。引入新依赖时必须确保协议兼容：

- **允许**：MIT、BSD-3-Clause、Apache-2.0、ISC、Python Software Foundation (PSF)、HPND 等 permissive 协议
- **禁止**：GPL-3.0、AGPL-3.0、LGPL-3.0 等传染性协议（任何代码引用即必须开源）
- **注意**： MPL-2.0、CDDL-1.0、EPL-2.0 等弱传染协议引入前需评估

当前依赖均为 permissive 协议（websockets/BSD-3-Clause, aiohttp/BSD-3-Clause, aiofiles/MIT, jieba/MIT, rank-bm25/MIT, openai/Apache-2.0, pillow/HPND, django/BSD-3-Clause, tomlkit/MIT）。

---

## 编码习惯与已知陷阱

- **文件写入必须原子化**：所有持久化存储（Markdown/JSON）必须写 `.tmp` → `os.replace()`，禁止直接覆写目标文件。已修复的 4 处：`markdown_store`、`important_memory_store`、`person_fact_store`、`character_card_service`。
- **async 函数中 `except Exception` 必须在前面加 `except asyncio.CancelledError: raise`**：Python 3.9+ 中 `CancelledError` 继承自 `Exception`，不加守卫会破坏 asyncio 取消协议。全项目已修复关键路径。
- **禁止用 `asyncio.CancelledError` 作业务流程控制**：应使用自定义异常（如 `StaleWindowError`），避免与任务取消混淆。
- **`Future.set_result()` 不要在持有 `asyncio.Lock` 时调用**：回调链路可能尝试获取同一把锁导致死锁。应收集 waiter → 锁外 resolve。
- **禁止在 `async` 上下文中使用同步阻塞 I/O**：`Path.read_text()` / `Path.write_text()` 应通过 `asyncio.to_thread()` 包裹，或使用 `aiofiles`。

---

## 标签常量

中文字符串标签统一在 `src/handlers/label_constants.py` 管理：
- `SESSION_TYPE_LABEL` — 私聊/群聊标签
- `SENDER_LABEL_USER` — 用户标签
- `SENDER_LABEL_ASSISTANT` — 助手标签
- `DISPLAY_NAME_FALLBACK` — 显示名称兜底值

修改这些字符串时只需改一处，所有引用自动同步。
