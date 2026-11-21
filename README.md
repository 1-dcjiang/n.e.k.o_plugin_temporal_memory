# temporal_memory · 时序记忆

N.E.K.O 插件：把对话压成**可核对原话的证据记忆**，而不是把历史全塞进提示词。

v0.4 的核心原则：

- **原话是证据**：每条记忆都带用户原句、来源事件 id、记录时间；没有原话的条目不进入召回。
- **不自动注入**：不调用 `push_message`，不往下一轮对话里塞任何内容。
- **按需拉取**：只有用户明确问到过去、模型调用 `recall_temporal_memory` 时，才返回仍然有效的条目。
- **到期休眠**：事件 90 天、偏好/事实 180 天没有再次确认就自动休眠；手动固定不衰减。
- **矛盾先待确认**：新旧说法冲突时两边暂停召回，面板里选一条采用，另一条归档；已固定的旧条目不会被冻结，新说法单独列为未分组待确认，可逐条保留或归档。
- **可纠正可撤销**：摘要/内容、类型和标签都能改，**原话证据只读**；纠正、固定、移除、冲突处理都留撤销快照，最多保留最近 1000 条。
- **召回内容当不可信数据**：整块包 `<memory_data untrusted>`，所有字段折行并转义；`remember` 默认不置顶，标签走白名单，写入按角色限速。
- **容量可回收**：5000 条上限只统计有效和待确认；面板移除、到期休眠立刻释放名额，另有「清理旧记忆」永久删除全部已加载角色库中已移除超过 30 天或距最后一次确认超过 365 天且未固定的条目。召回只增加召回计数，**不刷新「最后确认时间」**，休眠倒计时只认确认和纠正。

> 当前状态：v0.5.11 在 0.5.10 上修掉 A1341/A1342 核查确认的问题（文案纠错、逐库容错、清空回收、pin 冲突不冻结、工作记忆单条删除、关闭时不建空库、面板失败提示）；175 项测试（pytest 收集，含参数化；文件内 171 个 `def test_`）、`ruff`、`check --strict`、Hosted TSX、包内测试均通过。

## 目录

```
temporal_memory/
├── plugin.toml            # 插件元信息与配置项（v0.5.11）
├── config.example.toml    # 运行时配置模板
├── __init__.py            # 插件类：写入入口、按需召回、面板动作、生命周期、定时维护
├── memory_store.py        # sqlite：锚点链、FTS5、语义网、工作记忆、撤销台账
├── scoring.py             # 打分、摘要、标签、类型识别、冲突判定（纯规则）
├── retrieval.py           # 只读召回与字符预算裁剪
├── engine.py              # 调度器：observe / remember / recall / 休眠与分层
├── tests/                 # 单测、迁移测试、真实 SDK 注册测试
├── dist/                  # .neko-plugin 包
├── docs/
│   ├── guide.md           # 用户指南
│   └── design.md          # 设计取舍与验收口径
└── ui/settings.tsx        # 设置面板：记忆、证据、矛盾、纠正、撤销、清理、导出
```

## 安装

**开发者模式（推荐）**：把本目录注册为插件项目（目录名须等于 `plugin.id`，即 `temporal_memory`），在插件详情页 Reload 生效。源码放在任意目录即可，不需要放进 N.E.K.O 安装目录。

**打包**：在装有 N.E.K.O 源码与 `uv` 的机器上执行

```bash
uv run neko-plugin check temporal_memory --strict
uv run neko-plugin build temporal_memory --out temporal_memory.neko-plugin
```

## 入口

| 入口 | 调用方 | 何时调用 | 返回 |
|---|---|---|---|
| `observe` | 模型 | 每轮对话结束后 | 是否建锚点；只落本插件库，返回值仅供调用方核对证据 |
| `remember` | 模型 | 用户明确说「记住 / 别忘」 | 写入的锚点与原话证据；`pinned=true` 长期保留 |
| `recall_temporal_memory` | 模型 | 用户问「上次 / 之前 / 你还记得吗」或确实需要旧偏好 | 仅 active、带原话证据的摘要块 |
| `recall` / `timeline` | 设置页 | 面板预览 | `agent_hidden`，不向模型暴露 |
| `confirm_memory` / `pin_memory` / `correct_memory` / `archive_memory` | 设置页 | 用户核对、固定、纠正、移除 | 单条动作结果 + 撤销记录 |
| `resolve_conflict` | 设置页 | 用户处理矛盾 | 保留一条、其余归档 |
| `undo_last` / `export_memories` | 设置页 | 撤销或导出 | 撤销最近一次面板动作 / JSON |
| `delete_memory` / `clear_memories` | 设置页 | 单条永久删除或清空全部 | `agent_hidden`，需 `confirmed` |

## 注入方式（v0.4）

原版 `push_message(ai_behavior="read", visibility=[])` 已物理删除，插件里没有 `_push_read`、没有注入限流参数、没有 `injections` 台账。

现在的“注入”是拉取式：

1. 写入只落 `memory-<角色>-<hash8>.db`，不碰宿主记忆，也不往对话里写消息。
2. 模型需要旧上下文时调用 `recall_temporal_memory`，召回只挑 `status='active'` 且 `quote` 非空的条目。
3. 休眠、待确认冲突、已移除的条目不返回。
4. 召回结果按 `recall_char_budget` 截断；不会逐字念给用户。

## 生命周期

| 类型 | 默认休眠期 | 说明 |
|---|---|---|
| 事件 | 90 天 | 从最后一次确认/写入算起 |
| 偏好 | 180 天 | 同一偏好在再次出现时自动确认，不重复建条 |
| 决定 / 事实 | 180 天 | 与偏好共用非事件阈值 |
| 手动固定 | 不衰减 | `pinned=1`，不自动休眠，`effective_score` 恒为 1.0 |

- 自动休眠每小时由 `maintenance` 定时器执行。
- 再次出现同一偏好会刷新 `last_confirmed_at`，不新增重复条目。
- 面板「确认有效」也会唤醒休眠条目。

## 矛盾判定

规则保守，只处理能解释清楚的冲突：

- 偏好：对象相同、极性相反（「喜欢猫」vs「不喜欢猫/讨厌猫」）→ 两条都变 `pending`，同属一个 `conflict_group`。
- 事实：同一主语模板（「我住在…」「我叫…」「我的…是…」）但内容不同 → 待确认。
- 决定：同一前缀（决定/计划/打算/准备/约好）但目标不同（「决定去北京」vs「决定去上海」）→ 待确认。
- 其它：高度相似且否定词相反 → 待确认。

`pending` 条目不能直接确认或固定，必须在面板里选一条采用；另一条归档。处理动作可撤销。

## 面板

- 记忆列表：类型、状态、分数、角色、剩余休眠天数。
- 展开原话：`quote`、`source_kind`、`source_event_id`。
- 每条可确认、固定/取消、纠正（摘要/内容、类型、标签；原话只读）、移除。
- 矛盾组单独置顶，逐条「采用这条」。
- 「撤销上一步」只撤销最近一次动作，不会把别的角色库一起回滚。
- 「导出 JSON」包含 `anchors`、`semantics` 与最近 `working_window` 行 `working_memory`（默认 8 行），含原话和状态；单表最多 10000 条、合计 20000 条，超限会返回 `truncated` 与提示。
- 工作记忆卡片直接展示每个角色最近 8 行用户/助手原文（两行约一轮，8 行≈4 轮对话），每条可单独永久删除，并提示明文存储与模型传入。
- 单条可「永久删除」，不必等 30 天清理；「清空全部记忆」两次确认后清空数据目录里的所有角色库，包括当前没加载的库。
- 已移除条目单独列出，最多最近 120 条，可直接永久删除；总开关关闭后写入和召回都停用。

## 数据模型

| 表 | 作用 | 关键字段 |
|---|---|---|
| `anchors` | 时间线节点 | `ts, summary, quote, source_event_id, kind, status, score, last_confirmed_at, pinned, prev_id, next_id` |
| `anchors_fts` | 锚点全文索引 | FTS5，`trigram` 优先、回落 `unicode61`；短查询走 `LIKE` |
| `semantics` | 偏好/事实/决定 | `content, quote, kind, status, score, last_confirmed_at, conflict_group, attach_anchor_id` |
| `topics_edges` | 语义邻接 | `src, dst, weight`，按共享标签的 Jaccard 建边 |
| `wm` | 工作记忆环形窗口 | `ts, role, content`，默认保留 8 行（用户/助手各一行，约 4 轮） |
| `revisions` | 撤销台账 | `action, before_json, after_json, undone` |
| `meta` | 链表头尾指针 + 轮次计数 | `head` / `tail` / `turn_no` |

存储位置：插件私有数据目录，按角色分库为 `memory-<角色>-<sha256前8位>.db`；取不到角色名时沿用唯一已有的角色库，没有才用 `memory.db`，WAL 模式。老库启动时自动补列、回填 `quote=summary`，不会删数据；0.4.9 的 `memory-<角色>.db` 会在首次访问时迁到新哈希名。

## 配置

| 键 | 默认 | 说明 |
|---|---|---|
| `enabled` | `true` | 总开关，关闭后写入与召回一起停用（不建新库） |
| `working_window` | `8` | 工作记忆保留轮数 |
| `anchor_threshold` | `0.3` | 建锚点阈值，越高越稀疏 |
| `max_anchor_summary_chars` | `140` | 摘要上限；原话证据不受此限 |
| `hot_recall_window_days` | `7` | hot 判定的召回观察窗 |
| `cold_after_days` | `60` | 多久未召回降到 cold |
| `dormancy_event_days` | `90` | 事件自动休眠天数 |
| `dormancy_preference_days` | `180` | 偏好/事实/决定自动休眠天数 |
| `max_recall_anchors` | `6` | 单次召回锚点上限 |
| `max_recall_semantics` | `12` | 单次召回偏好/事实上限 |
| `recall_char_budget` | `1200` | 按需召回字符预算 |

## 打分规则

写入路径零模型调用，只认五类信号：

| 信号 | 权重 | 例子 |
|---|---|---|
| 显式记忆 | 0.45 | 「记住」「别忘」「很重要」 |
| 情绪波动 | 0.30 | 「焦虑」「崩溃」「特别开心」，强情绪额外 +0.15 |
| 首次知识 | 0.30 | 「第一次配置」「报错」「不兼容」 |
| 决策计划 | 0.25 | 「决定」「报名」「截止」「下周」 |
| 任务动作 | 0.10 | 「帮我排查」「提交」 |

疑问句、寒暄、极短句归零，只进工作记忆、不占时间线。偏好/事实片段不受锚点阈值限制，会单独留证据。

## 验收口径

1. 100 轮日常对话产出 3–8 个锚点。
2. 随机删除 20 个锚点后，链表无断点、无悬空指针。
3. 固定锚点不参与降级；事件 90 天、偏好 180 天后自动休眠。
4. 矛盾未处理前两边都不召回；处理后只有保留的一条 active。
5. 面板纠正、固定、移除、冲突处理都能撤销。

## 许可

MIT，见根目录 `LICENSE`。
