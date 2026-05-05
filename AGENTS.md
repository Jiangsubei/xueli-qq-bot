# AGENTS.md

---

## 通用原则

### 实现新规则前必须检查复用可能性

添加任何新的决策逻辑、条件分支或启发式规则之前，必须完成以下检查步骤：

- 检索已有规则
- 确认无等价或重叠规则
- 优先扩展而非新建

### 编码陷阱（必读）

- **async `except Exception` 前必须先 `except asyncio.CancelledError: raise`**：Python 3.9+ 中 `CancelledError` 继承自 `Exception`，不加守卫会破坏 asyncio 取消协议
- **禁止用 `asyncio.CancelledError` 作业务流程控制**：用自定义异常（如 `业务自定义异常`）代替
- **文件写入必须原子化**：所有持久化（Markdown/JSON）先写 `.tmp` 再 `os.replace()`，禁止直接覆写目标文件
- **`Future.set_result()` 不要在持有 `asyncio.Lock` 时调用**：回调可能尝试获取同一把锁导致死锁，应收集 waiter → 锁外 resolve
- **禁止在 async 上下文中使用同步阻塞 I/O**：`Path.read_text()` / `Path.write_text()` 通过 `asyncio.to_thread()` 包裹或用 `aiofiles`

### 依赖协议

MIT 许可证。引入新依赖必须 permissive 协议（MIT、BSD-3-Clause、Apache-2.0、ISC、PSF、HPND）。**禁止** GPL-3.0/AGPL-3.0/LGPL-3.0。

### 日志规范

#### 格式要求
- `[模块]` 格式日志仅输出简单内容，**禁止**结构化参数（如 `extra={}`）
- 日志级别：`INFO` 用于关键节点，`DEBUG` 用于详细调试

#### 必须保留的日志
- `{关键日志标签}` — 完整提示词
- `{关键日志标签}` — 提示词摘要
- HTTP 访问日志（标准格式）
- AI 重试日志（重试次数/延迟）
- `{关键日志标签}` — 启动信息

#### 禁止出现的日志
- 规划原始 DEBUG 日志（包含 `plan.action` / `plan.reason` 等）
- 非关键路径的 DEBUG/INFO 心跳日志
- 用户侧异常解释性文字

### 提示词模板规范

所有提示词必须抽象到 `{项目prompts目录}/` 目录下的 `.prompt` 模板文件，**禁止**在 Python 代码中硬编码提示词字符串。

涉及提示词生成的模块必须使用 `PromptTemplateLoader` 加载模板，不得内嵌字符串。

#### 提示词内容一致性原则（强制）

- **所见即所得**：日志中出现的占位符必须与模型看到的文字一致，不允许日志用 `or "[空]"` 缩写而模型用完整语义文字
- **口径统一**：Timing Gate / Planner / Reply 三个模型的同一语义描述必须用完全相同的文字
- **图片描述口径**：成功识别统一为 `[图片] {merged_description}`；识别失败统一为 `[图片]未成功识别`；逐图描述格式为 `第N张: xxx`
- **空文本占位符**：用户发送空文本时统一表述为 `用户发送了空文本`，不用 `[空]` 等缩写

---

## 项目规范

### 常用命令

```bash
uv pip install -r requirements.txt

# 全量测试（使用虚拟环境 Python）
.venv/bin/python -m unittest discover -s xueli/tests -t xueli

# 单测试文件
.venv/bin/python -m unittest discover -s xueli/tests -t xueli -p test_conversation_planner.py

# 启动
start.bat / start.ps1  # Windows
bash start.sh         # Linux/macOS
```

- 无 lint/typecheck/formatter 配置，勿运行 ruff/mypy/pylint
- 测试框架是 `unittest`（非 pytest）
- 测试以 `-t xueli` 设定包根，import 路径为 `src.*`（如 `from src.core.models`），**不是** `xueli.src.*`
- `xueli/config/config.toml` 为本地私有配置（gitignore），首次运行需从 `config.example.toml` 复制

---

### 核心架构

#### 主消息处理链

```
NapCatConnection (WebSocket)
  └── 消息队列解耦 + 消费节流 (50ms) + Notice背压 (maxsize=100)
        ↓
MessageHandler
  └── PlanningWindowService (缓冲窗口调度)
        └── ConversationPlanner (reply/wait/ignore + PromptPlan)
              └── TimingGateService (continue/wait/no_reply)
                    └── ConversationContextBuilder
                          └── ReplyPipeline (PromptPlan + 上下文 → 最终回复)
                                └── ReplyGenerationService (AI 生成)
                                      └── MemoryFlowService (记忆写入，async queue infrastructure)
```

#### 背压机制层级

| 层级 | 机制 | 参数 | 效果 |
|------|------|------|------|
| Adapter | 消费节流 | 50ms/条 | 限制接收速率 |
| Adapter | 有界通知队列 | maxsize=100 | Notice 事件背压 |
| Session | 触发阈值 | 1~N 条消息 | 消息聚合 |
| Session | 静默期 | 50ms | 等待消息平静 |
| Session | @打断 | `_force_timing_continue` | 高优先级插队 |
| 循环 | 最大轮次 | 6 轮/次 | 限制单次占用 |

#### 关键实现细节

**NapCatConnection 队列解耦** (`src/adapters/napcat/connection.py`)
- `_receive_loop`: 接收消息写入队列
- `_consume_loop`: 消费队列 + 50ms 节流
- `_notice_loop`: 独立 Notice 队列 (maxsize=100)

**@打断机制** (`planning_window_service.py`)
- `_should_bypass_window` 返回 `(bypass, force_continue)` 元组
- @mention 不再绕过窗口，而是设置 `planning_signals["_force_timing_continue"]`
- `TimingGateService._fallback_decision` 消费该信号，强制 continue

**会话窗口调度** (`conversation_window_scheduler.py`)
- `BufferedWindow.min_messages`: 数量触发阈值
- `ConversationWindowState.last_trigger_at`: 上次触发时间
- `ConversationWindowState.average_reply_latency`: 平均回复延迟（默认 5.0s）
- `mark_window_complete` 后 50ms 静默期 Debounce

**运行时最大轮次** (`runtime.py`)
- 主循环 `for round_index in range(6)` 最多执行 6 轮

#### 关键契约

**PromptPlan**：planner 输出给 reply pipeline 的核心结构，含 `action` (reply/wait/ignore) 和各上下文层开关。改动它必须同步更新所有调用方、测试和文档。

#### 三大提示词模板

- `xueli/prompts/zh-CN/planner.prompt` — reply/wait/ignore 决策
- `xueli/prompts/zh-CN/timing_gate.prompt` — continue/wait/no_reply 决策
- `xueli/prompts/zh-CN/reply.prompt` — 最终回复生成

主模板文件 + 代码内 section 注入。改 reply prompt 优先改 section/renderer/style policy，不退回大段字符串硬拼。

#### Adapter 隔离

- 平台适配器在 `src/adapters/` 下：`napcat/`（QQ/NapCat WebSocket）、`api/`（HTTP API runtime）
- 新 adapter 必须实现 `attach_inbound_event()`，由 `dispatcher.py` 优先调用
- OneBot 归一化在 `adapters/napcat/normalizer.py`，**不在** core
- core 不应被 QQ/NapCat 细节污染

---

### 记忆系统

- 记忆写入统一经由 `MemoryFlowService`（**不在** ReplyPipeline 内）
- 三层记忆：person_fact / chat_summary / conversation_recall
- 存储：Markdown 明文 + SQLite；检索：BM25 初排 + 向量联想（n-gram 余弦，零外部依赖）
- 拟人化特性：动态遗忘（用进废退）、软遗忘（归档记忆打折召回）、情绪标记、离线消化、语义联想
- `MemoryFlowService` 内置 `asyncio.Queue(maxsize=256)` 基础设施，队列满时丢弃旧任务

#### 记忆隔离原则（强制）

**存储路径与检索路径必须对称**
- 写入时 `storage_user_id = f"group:{group_id}:{user_id}"`，检索时也必须用同样的 ID
- 检索层（`get_scope_user_ids`）必须根据 `message_type` 和 `group_id` 返回正确的存储 ID 列表

**索引重建必须覆盖全量用户**
- `rebuild_all_indices()` 必须使用 `storage.get_user_ids()` 获取完整列表，不能自己 glob 文件系统
- `get_user_ids()` 返回什么，索引就必须扫什么

**作用域匹配不能产生歧义**
- `GROUP + ""` 在 `group_id=""` 时会错误匹配（如私聊场景）
- 无有效 `source_group_id` 时应回退到 `DEFAULT` scope，不能制造空字符串的 GROUP scope

**同类型存储组件行为必须一致**
- `MarkdownMemoryStore` 如何处理 `group:` 前缀路径，`ImportantMemoryStore` 也必须一样
- 否则一个能找到群聊记忆，另一个找不到

#### 记忆存储目录结构

```
data/memories/
├── users/                           # MarkdownMemoryStore
│   ├── {user_id}.md                # 私聊记忆
│   └── group/
│       └── {group_id}:{user_id}.md # 群聊记忆
├── important/                      # ImportantMemoryStore
│   ├── {user_id}.md                # 私聊重要记忆
│   └── group/
│       └── {group_id}:{user_id}.md # 群聊重要记忆
├── archive/users/                   # 冷记忆归档
│   └── {user_id}.md / group/
├── conversations/                  # SQLite 对话历史
├── person_facts/                   # 人物事实 JSON
├── _fact_evidence/                 # 记忆争议证据
└── _character_cards/              # 人设/亲密度
```

---

### 关键约束

1. 私聊和群聊共用一条 conversation 主链，**不要**分裂两套逻辑
2. `ReplyPipeline` 定位是 prompt compiler，回复后副作用走 `MemoryFlowService`
3. 命名用 `conversation_*`、`adapter_*`、`platform_*` 等中性命名，**不要**扩散 `group_*`、`napcat_*`
4. 会话永不过期，重启后从历史存储恢复并保留原始时间信息
5. 结构化分段发送是主路径（模型输出 JSON 数组）；正则分句仅作兜底
6. 普通图片只做视觉理解，**不**入 emoji 仓库；原生表情只存 `face / mface` 引用
7. `data/` 目录是运行时产物，已 gitignore，不提交
8. `group_reply_decision` 配置未填写时，群聊退回规则路径（通常只在被 @ 时回复）
9. **用户侧异常提醒原则**：处理失败时不要发送解释性文字给用户（QQ 场景不需要"抱歉处理失败了"这类消息），静默失败即可；原则是判断这个提醒是否会让用户觉得突兀

---

### 高风险改动

以下模块改动必须连带检查测试和所有调用点：

- `PromptPlan` / `ConversationPlanner` / `TimingGateService`
- `ReplyPipeline` / `ReplyPromptRenderer`
- `MessageHandler`
- `MemoryManager` / `MemoryFlowService`
- `BotRuntime` 主消息处理链
- 三大 prompt 模板文件

---

### 标签常量

中文字符串标签统一在 `src/handlers/label_constants.py` 管理：

- `SESSION_TYPE_LABEL` — 私聊/群聊标签
- `SENDER_LABEL_USER` / `SENDER_LABEL_ASSISTANT` — 用户/助手标签
- `DISPLAY_NAME_FALLBACK` — 显示名称兜底值

---

### 已模板化的提示词（供参考）

- `planner.prompt` — 对话规划器
- `timing_gate.prompt` — 节奏门控
- `reply.prompt` — 回复生成
- `reply_constraint.prompt` — 回复格式约束
- `insight_digestion.prompt` — 离线消化
- `vision.prompt` — 图片理解
- `vision_emotion.prompt` — 表情分类
- `emoji_reply.prompt` — 表情追评决策
- `rerank.prompt` — 记忆重排
- `reflection.prompt` — 记忆冲突判断
