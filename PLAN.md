# temporal_memory 实施计划（v0.4）

> 范围：回滚到 0.1.0 原版基线，取消 `push_message` 被动注入，改为证据优先 + 按需召回。
> 使用说明见 README / docs/guide.md，设计取舍见 docs/design.md。

## 1. 目标与不做什么

**交付一个可安装的 N.E.K.O 插件**，行为是：

- 每轮对话写工作记忆，达阈才留事件锚点；偏好/事实片段单独留原话证据。
- 每条记忆带 `quote`、`source_event_id`、`kind`、`status`、`last_confirmed_at`。
- 事件 90 天、偏好/事实 180 天未确认自动休眠；手动固定不衰减。
- 矛盾先标 `pending`，面板选一条才恢复召回。
- 不调用 `push_message`；模型只在用户问过去时调用 `recall_temporal_memory` 拉取。

**不做**：体素 / Morton 空间索引、L2/L3 摘要金字塔、前瞻记忆、回忆重构、自动 `@message` 钩子、模型摘要。

## 2. 里程碑

| 阶段 | 交付物 | 完成标准（DoD） | 状态 |
|---|---|---|---|
| **M0 回滚** | 从 `dist/temporal_memory-0.1.0.neko-plugin` 恢复原版源码 | 67 项原测试通过 | ✅ |
| **M1 数据层** | `quote/source/kind/status/score/confirm/conflict` 字段、`revisions`、老库迁移 | 迁移测试通过 | ✅ |
| **M2 生命周期** | 90/180 天休眠、固定豁免、reconfirm、pending 冲突 | engine/store 测试通过 | ✅ |
| **M3 取消注入** | 删 `injection.py`、`_push_read`、inject 配置；`recall_tool` 只读 active | 源码无 `self.push_message` | ✅ |
| **M4 面板** | 证据展开、确认、固定、纠正、移除、冲突、撤销、导出 | TSX 落盘，check 通过 | ✅ |
| **M5 验证** | pytest、ruff、check --strict、build、包内测试 | 全绿 | ✅ |
| **M6 联调** | 客户端导入包，观察休眠/冲突/撤销真实行为 | 真实对话验证 | 待客户端验收 |

## 3. 文件级工作分解

| 文件 | 内容 | 状态 |
|---|---|---|
| `memory_store.py` | 新字段、`revisions`、生命周期、冲突、撤销、迁移 | ✅ |
| `scoring.py` | 类型识别、原话片段、冲突判定 | ✅ |
| `engine.py` | 证据写入、reconfirm、休眠、面板动作、导出 | ✅ |
| `retrieval.py` | active + quote 过滤、预算裁剪 | ✅ |
| `__init__.py` | 写入/召回入口、面板动作、维护定时器 | ✅ |
| `plugin.toml` | v0.4.3、11 个 settings、无 inject 键 | ✅ |
| `ui/settings.tsx` | 记忆/证据/冲突/纠正/撤销/导出 | ✅ |
| `README.md`、`docs/guide.md`、`docs/design.md` | v0.4 口径 | ✅ |
| `tests/` | 注入删除、证据、休眠、冲突、迁移、撤销、真实 SDK | ✅ |

## 4. 接口契约

| 入口 | 必填 | 可选 | 返回要点 |
|---|---|---|---|
| `observe` | `user_text` | `assistant_text`、`source_event_id`、`ts` | `score`、`anchored`、`anchor`、`semantics`，无 `injection` |
| `remember` | `text` | `source_event_id`、`ts`、`pinned`、`tags` | `anchor`、`kind`，默认不固定 |
| `recall_temporal_memory` | `query` | — | `text`、`chars`、`anchors`、`semantics` |
| `recall` / `timeline` | 视参数 | — | `agent_hidden`，仅设置页 |
| `confirm_memory` / `pin_memory` / `correct_memory` / `archive_memory` | `table`、`id` | 纠正字段 | 动作结果 + revision |
| `resolve_conflict` | `group`、`keep_id` | — | 保留/归档结果 |
| `undo_last` | — | — | 最近一条 revision |
| `export_memories` | — | — | anchors + semantics JSON |

## 5. 验收

1. **锚点产出率**：100 轮日常对话 → 3–8 个锚点。
2. **链完整性**：随机删 20 个锚点后 `chain_integrity()` 无 problem。
3. **休眠**：事件 91 天 dormant、偏好 91 天仍 active、181 天 dormant、固定永远 active。
4. **矛盾**：未处理前两边都不召回；处理后只有保留一条 active。
5. **撤销**：纠正、固定、移除、冲突处理都能恢复。
6. **无注入**：源码无 `self.push_message`，`observe` 返回无 `injection` 键。

## 6. 风险与缓解

| 风险 | 触发条件 | 缓解 |
|---|---|---|
| 规则冲突漏判 | 复杂语义矛盾 | 面板人工纠正；保守判定不自动覆盖 |
| 休眠误伤 | 用户长期不提旧事 | 面板确认/固定；原话不删 |
| 撤销串库 | 多角色库都有 revision | 只撤销 revision 时间最新的一条 |
| 证据缺失 | 老库/手工导入 | 迁移回填 `quote=summary`；召回要求 quote 非空 |
| UI 未实机 | hosted-tsx 的 Blob/URL 差异 | M6 在客户端验证导出 |

## 7. A1261 增量审返修（0.4.1）

- `processed_events` 原子占位覆盖“只写语义、不建锚点”的事件；`chat_turn` 与 `manual` 分开。
- `retier` 的 hot 保护改用 `created_at`。
- 语义邻居每标签 200、总量 400。
- 撤销同一秒内按 SQLite `rowid` 排序。
- `journal_mode=WAL` 只设一次；连接仍按语句短开，保留 Windows 迁移安全。
- 原话证据只读，面板纠正只能改摘要/内容、类型和标签。
- 版本 0.4.1，新增 `CHANGELOG.md`。

## 8. A1263 增量审返修（0.4.2）

- `observe` / `remember` 从「先提交占位」改为占位、wm、锚点、语义同一 `BEGIN IMMEDIATE` 事务；异常整体回滚。
- 锚点与语义增加事件级唯一索引；`_insert_anchor` / `_insert_semantic` 对同事件重复写入返回已有行。
- `processed_events` 增加 365 天清理，维护任务顺手执行。
- 邻居配额按标签数均分总量，三个及以上标签不会被前两个饿死。
- `event_semantics` 记录本次事件触碰的语义（含 reconfirm 旧条），重复事件返回值不再漏项。
- 面板纠正新增标签输入，后端接受逗号分隔标签并重建 FTS/邻接边。
- 版本 0.4.2，README 测试数改为 101。

## 9. A1265 增量审返修（0.4.3）

- 删除死代码 `release_event`，事务回滚是唯一失败恢复路径。
- 标签纠正改为同一事务内更新 + 撤销快照 + 重建 FTS/邻接边。
- 撤销标签纠正后补重建语义邻接边。
- 标签存储统一排序去重。
- 旧库重复行导致唯一索引跳过时，状态卡 warning，不再静默吞 `OperationalError`。
- 事件索引清理只删空事件；有记忆残留的事件保留占位。
- 版本 0.4.3，README 测试数改为 103。

## 10. A1266 增量审返修（0.4.4）

- `undo_last` 整个动作包进 `transaction()`，字段回滚、FTS 重建、语义边重建与 `revisions.undone` 同一提交。
- `revise_memory` 的旧行在事务内读取，撤销快照与更新一致。
- `stats()` 发现唯一索引缺失时重试；用户清掉重复行后无需重启。
- 补 `idx_anchors_source_event` / `idx_semantics_source_event` 非唯一索引，旧库缺唯一索引时清理查询仍走索引。
- `processed_events` 上限 20000；超限删最旧，`observe` / `remember` 在占位丢失时按已有记忆判重。
- 版本 0.4.4，README 测试数改为 105。

## 11. A1267 最终增量审收尾（0.4.5）

- 唯一索引重试加 300 秒冷却；被锁时吞 `OperationalError`，面板刷新不再报错。
- 唯一索引建成后删同列非唯一索引，减少冗余写放大；失败时保留非唯一索引供 `NOT EXISTS` 使用。
- 补 `undo_last` / `revise_memory` 失败回滚测试、`stats()` 遇锁与冷却测试、EXPLAIN 索引断言。
- 版本 0.4.5，README 测试数改为 108。

## 12. A1268 索引回归修复（0.4.6）

- 撤回“唯一索引建成后删非唯一索引”：部分唯一索引的 `WHERE source_event_id<>''` 使只写 `source_event_id=?` 的查询用不上它。
- 非唯一事件索引只在唯一索引失败时补建，健康库不再启动白建白删。
- 事件查询补 `source_event_id<>''`，健康库走部分唯一索引；旧库缺唯一索引仍走非唯一索引。
- EXPLAIN 断言区分健康库/旧库两条路径。
- 版本 0.4.6，测试数保持 108。

## 13. A1269 低项收尾（0.4.7）

- 唯一索引恢复后清理旧版本遗留的非唯一回退索引。
- 事件查询 SQL 抽成模块常量，store 与 EXPLAIN 测试共用，消除手抄漂移。
- EXPLAIN 覆盖 `semantics_for_event` UNION 子查询和真实 prune `DELETE`。
- 启动发现唯一索引缺失时先置冷却，首次刷新不立刻重试。
- 版本 0.4.7，测试数 109 项 / 105 个 `def test_`。

## 14. A1270 低项收尾（0.4.8）

- 唯一索引 DDL 加 savepoint，两条索引要么都成功，要么整体回滚。
- 查询常量补齐 `ORDER BY` / `LIMIT`，EXPLAIN 与真实方法共用完整语句。
- EXPLAIN 覆盖 `semantics_for_event` 的无 `source_kind` 分支。
- 补启动清理遗留回退索引、DDL 半途失败回滚两条测试。
- 版本 0.4.8，测试数 111 项 / 107 个 `def test_`。

## 15. A1271 安全审查修复（0.4.9）

- H1：召回整块包不可信数据标签，所有字段折行/转义，`quote` 恒加围栏。
- M1：`remember` 默认不置顶；标签白名单；`ts` 夹范围；每角色写入限速；角色库数量上限。
- L1：`undo_last` 表名过白名单。L2：导出明文提示。L3：回给模型的异常用通用文案。L4：捕获 `OverflowError`。L5：修正面板误导文案。
- 版本 0.4.9，测试数 112 项 / 110 个 `def test_`。

## 16. A1274 安全加固（0.5.0）

- 事件 id 按 `chat_turn:` / `manual:` 分命名空间；重复事件只返回 id，不返回旧正文。
- 召回字段先截断再拼围栏，保证二次裁剪不切断 `</quote>`。
- 单库总量 5000 条上限，超限整笔回滚并返回 `skipped='capacity'`；邻居上限 50/200。
- 角色库名加 sha256 前 8 位，迁移旧 `memory-<角色>.db` 并写 `meta.character`。
- Schema v4 迁移旧事件 id；导出 `schema=4`。
- `MAX_ENGINES` 到顶给出明确清理提示；`observe` 文案改成“返回值供核对证据”。
- 版本 0.5.0，测试数 124 项 / 120 个 `def test_`。

## 17. A1289 增量快审收口（0.5.1）

- 容量只统计 `active` / `pending`，面板移除、到期休眠立即释放写入名额。
- 新增 `purge_stale` 与 `delete_semantic`：永久删除 30 天前已移除、365 天前休眠且未固定的条目；插件入口 `purge_memories` 与面板「清理旧记忆」二次确认。
- 导出 `schema=4`；相似语义复认只返回 `id/kind/status/confirmed`，不再回旧 `quote/content`。
- v4 迁移的 `anchors` / `semantics` 改 `UPDATE OR IGNORE`，碰撞时跳过单行而不是整库打不开。
- 邻居注释改回 200/50。
- M2 前缀抢占：`observe` 入口拒绝自带 `chat_turn:` / `manual:` 前缀的 `source_event_id`；宿主 `message_id` 与 `event_id` 均为 UUID4，正常上下文也猜不到原始 id。
- 版本 0.5.1，测试数 131 项 / 127 个 `def test_`。

## 18. A1291 增量快审收尾（0.5.2）

- `purge_stale` 选取与删除放进单个 `BEGIN IMMEDIATE` 事务；删除入口带 `stale_cutoffs` 复查 `pinned/status/时间`，中途失败整批回滚。
- `revisions` 的 `before_json/after_json` 同步剔除已永久删除的 id，避免 `quote/content` 残留。
- `undo_last` 按 `rowcount` 计恢复数；目标全缺失返回 `target_missing`，不再假装成功。
- `confirm/pin/undo` 唤醒 `archived/dormant` 前检查容量，达到 5000 时拒绝并给明确错误。
- 新增 archived/dormant 插入不计容量；面板记忆列表上限 80 → 240；清理文案改为“距最后一次确认超过 365 天”。
- 版本 0.5.2，测试数 139 项 / 135 个 `def test_`。

## 19. A1292 增量快审收尾（0.5.3）

- 清理后整条快照只剩被删条目时留空墓碑，撤销先命中并报“已被清理”，不再静默回滚更早的无关动作。
- `revisions` 最多保留 1000 条；写入和清理前裁剪，清理改逐行游标扫描，缩短写锁持有时间。
- 休眠条目要求 `last_confirmed_at > 0` 才参与 365 天清理，异常零值老数据不误删。
- 删除 `_memory_action` 中走不到的 `target_missing` 分支；撤销缺目标提示补“或已被清理”。
- 面板列表被截断时显示“仅最近 240 条”的提示，明确处理后会继续露出更早条目。
- 版本 0.5.3，测试数 141 项 / 137 个 `def test_`。

## 20. A1293 增量快审收尾（0.5.4）

- 空墓碑保留原 `undone` 状态：已撤销 revision 不会重新变成可撤销；未撤销的仍先报“已被清理”。
- 面板快照按 `active/dormant` 过滤后再截断，archived/pending 不再占 240 窗口；截断提示按真正可见条数判断。
- `all_memories` / `list_memories` 增加状态过滤参数，SQL 参数化，默认全量行为不变。
- 版本 0.5.4，测试数 143 项 / 139 个 `def test_`。

## 21. A1294 增量快审收尾（0.5.5）

- `all_memories(statuses=())` 显式返回空结果；只有 `statuses=None` 表示不过滤，语义不再含糊。
- 面板快照 SQL 改为 `pinned DESC, 时间 DESC`，`engine.list_memories` 与 `_settings_snapshot` 的二次排序也改为固定优先，固定条目优先进入 300 条查询窗口，再截断到 240 条显示。
- 新增 `orphan_pending`：没有 `conflict_group` 的 pending 单独聚合，面板新增“未分组待确认”区，可“保留这条 / 归档这条”。
- 新增插件入口 `resolve_orphan(table, id, keep)`；保留转 active，归档转 archived，唤醒时检查 5000 容量。
- 冲突组和未分组待确认超过 120 条时给出截断提示；空墓碑留到 1000 条裁剪确认为无害，未改。
- 版本 0.5.5，测试数 147 项 / 143 个 `def test_`。

## 22. A1297 增量快审收尾（0.5.6）

- 每个角色库的 `orphan_pending` 取 121 条再判断，单库超过 120 条时 `orphan_pending_truncated` 也会为真。
- `resolve_orphan` 在 `set_memory_state` 事务内校验 `status='pending'` 且 `conflict_group=''`，误传 active/冲突组条目返回 `not_orphan`，不会绕开冲突处理。
- 面板截断文案改为“固定条目优先，其余按时间”，明确取消固定才会露出更多。
- 冲突列表按组截断：前 120 组完整保留，不再切出半个组。
- 单库查询窗口排序改为 `pinned DESC, active DESC, 时间 DESC`，较新的 dormant 不再挤掉更早的 active。
- 版本 0.5.6，测试数 151 项 / 147 个 `def test_`。

## 23. A1299 增量快审收尾（0.5.7）

- 冲突组去重键改为 `(角色库, group)`；同名 group 出现在两个角色库时分别显示，不再合成一组。
- `resolve_conflict` 增加可选 `character` 参数，只处理指定角色库；前端“采用这条”传入条目所属角色。
- `all_memories` 支持 `limit=None`，`export` 改为无上限读取，去掉 10000 条静默截断。
- 未分组待确认合并多库后按时间全局升序，再切前 120 条，不再按角色库顺序展示。
- 版本 0.5.7，测试数 154 项 / 150 个 `def test_`。

## 24. A1302 增量快审收尾（0.5.8）

- 导出加回上限保护：单表 10000 条、所有角色库合计 20000 条；超限返回 `truncated` 和 `warning`，不再静默截断。
- `resolve_conflict` 保留各库失败原因，`keep_not_in_group` 单独提示，不再和 `not_found` 混在一起。
- `character` 用 `None` 表示未指定，空字符串会定位空角色库，不再回退全库。
- 未分组待确认的截断提示写明“最早 120 条”，保留最早优先的设计取舍。
- 版本 0.5.8，测试数 158 项 / 154 个 `def test_`。


## 25. A1309 功能与红线核查收尾（0.5.9）

- 工作记忆进入面板与导出：每个角色最近 8 轮原文可见，导出的 `working_memory` 合并所有角色库。
- 单条永久删除：`delete_memory(table, id, confirmed)` 立即删除 anchor/semantic 及关联，清理撤销快照，可撤销记录标成墓碑。
- 清空全部：`clear_memories(confirmed)` 单事务清七张表并重建 FTS，返回各表计数；面板两次确认。
- 面板明示明文 sqlite、工作记忆可见、原话由模型传入；写入来源真实性仍无法在插件侧逐字核对，保留为已知边界。
- 迁移老库 `_adopt_legacy_db` 的归属启发式保留：多角色安装时不自动猜测归属，只做单角色场景的兼容迁移。
- 版本 0.5.9，测试数 161 项 / 157 个 `def test_`。

## 26. A1313 功能与红线核查收尾（0.5.10）

- 总开关同时管召回：关闭后模型侧 `recall_temporal_memory` 返回空结果并带 `disabled`，面板预览返回“总开关已关闭”，不再只拦写入。
- `clear_memories` 枚举数据目录里 `memory.db` 和全部 `memory-*.db`，未加载库也会开库清空并关闭；返回清空的 `databases` 数与失败数。
- `delete_memory` 增加可选 `character` 参数，面板传条目所属角色；未加载的角色库会临时打开删除后再关闭。
- 面板新增“已移除的记忆（可永久删除）”，最近 120 条，解决 archived 条目只能等 30 天清理的问题。
- 版本 0.5.10，测试数 165 项 / 161 个 `def test_`。

## 27. A1341 / A1342 核查收尾（0.5.11）

- 文案纠错：`i18n`、README、`docs/guide.md`、面板统一改成“关闭总开关后写入与召回一起停用”“工作记忆 8 行≈4 轮”。
- `delete_memory` 逐库容错：单库异常只记账，不再中止整个查找；返回 `failed_databases`。
- 未加载库的 `MemoryEngine(...)` 打开统一走 `asyncio.to_thread`（`delete_memory` / `clear_memories` / `delete_working_turn`）。
- `clear_all` 后做 `wal_checkpoint(TRUNCATE)` + `VACUUM`；清空全部成功后连带删除 `*.migrated` 迁移备份（有失败时保留）。
- `materialize_conflict` 遇到 `pinned=1` 旧条不再置 `pending`：旧条保持 active，新说法挂未分组待确认；撤销快照只回滚新条。
- 工作记忆单条永久删除：`delete_working_turn` 入口 + 面板按钮。
- 总开关关闭时 `observe`/`remember` 先判开关再取引擎，不再建空库。
- 面板 `act()` 读取返回值，清空/清理部分失败时提示失败库数。
- 已知限制（写入文档）：召回只涨 `recall_count`/`last_recalled`，不刷新 `last_confirmed_at`；启动仍只挂载老库或已有 owner 的角色库，纯角色库要等首次调用才加载，面板/导出只覆盖已加载库。
- 版本 0.5.11，测试数 175 项 / 171 个 `def test_`。

## 28. A1343 复核收尾（0.5.12）

- `delete_working_turn`：`character` 直接定位单库，`''`=默认库；不再 `or None` 跨库遍历，避免删到别库同 id 的 wm 行。
- `clear_all` 回报 `reclaimed`，`_shrink_file` 返回 checkpoint/vacuum 明细；面板提示“磁盘未回收”。
- `purge_memories` 逐库容错并回 `failed`/`databases`，不再一库异常整体报错。
- `materialize_conflict` 的 pinned 分支复用已有未分组待确认，同一条新说法不重复堆积。
- 总开关关闭时维护定时器直接返回 `skipped: disabled`。
- 文案与面板：`ui.strategy.enabled`、清空确认补迁移备份、工作记忆提示跟随 `working_window`、未分组区说明固定旧条仍生效。
- 已知限制补充：单条删除不做 VACUUM，只有清空全部回收文件。
- 版本 0.5.12，测试数 181 项 / 177 个 `def test_`。

## 29. 边界项按需化（0.5.13）

- 未加载角色库：只读探针（`mode=ro`，30 秒缓存）+ 面板列表 + `attach_database` 按需载入；不动启动全量挂载，不加 wm↔记忆条关联列。
- 导出 `export_memories` 改为枚举 `_db_file_paths()`，未加载库临时打开读完就关，补 `databases` 与 `failed_databases`。
- 原话核对：`scoring.quote_matches` 归一化包含 + 相似度兜底；`observe` 的 fragment、`remember` 对工作记忆用户行各自核对，不匹配降级未分组待确认并回 `quote_verified=false`；无对照样本时不做判断。
- 删除联动：`delete_memory` 成功后按 quote 匹配 `wm`，回 `working_memory_hits`；面板二次确认后逐行 `delete_working_turn`。
- 待确认可见性：`pending_alert_days` / `pending_overdue` / `pending_oldest_days` 进面板快照，状态栏提醒超 7 天未处理。
- 存储：`PRAGMA secure_delete=ON`，单条删除零覆盖；清空全部仍 checkpoint + VACUUM。
- 版本 0.5.13，测试数 194 项 / 190 个 `def test_`。

## 30. 上架审查整改（0.5.14）

- 描述层：README「使用前提（必读）」+ `plugin.toml` description/short_description + `i18n` 的 plugin 与 `entry.observe` 描述，统一写明「靠模型每轮调用 observe、覆盖不保证、原话由模型提交、对话明文落本地、召回是拉取式」。
- 面板 i18n：把所有硬编码中文换成 `props.t("ui.*")`；两个 locale 键集同构（192 键），补全 6 个缺失入口文案；面板与 i18n 用测试互相锁定（引用必须存在、不许有死键、除注释外不许出现中文）。
- 维护容错：`_run_maintenance` 包住逐库 `engine.maintenance`，异常只记警告与 `_maintenance_errors`；启动与定时器都不再被单个坏库打断，恢复后自动清除错误记录。
- 版本 0.5.14，测试数 205 项 / 199 个 `def test_`。

## 31. A1351 复核轻项（0.5.15）

- 面板窗口跟随：`_settings_snapshot` 改用 `self._working_window()`（读 `working_window`、`None` 补 8、坏值回 8、夹 0–50），面板去掉 `slice(0, 40)`，窗口调大后续行可见可删；窗口为 0 如实显示 0 行。
- 启动维护受总开关约束：`on_startup` 里逐库 `_run_maintenance` 包 `if self._settings.get('enabled', True)`，与定时器同口径。
- 删除联动给预览：`workmem_hits` 是文本相似匹配，面板弹窗列「角色 ｜ 时间 ／ 原文（60 字内，最多 8 行）」，i18n 新增 `ui.confirm.working_hits_line`，message 注明可能命中相近的行。
- null 不遍历：`delete_memory` / `delete_working_turn` 把 `None` 归一化成空串；`_delete_candidates(character: str)` 删除“全库排一遍”分支，两处 `input_schema` 文案同步。目标库打不开直接报错，不再翻其他库。
- 版本 0.5.15，测试数 215 项（214 passed + 1 skipped）/ 209 个 `def test_`。

## 32. 读取路径容错与口径收紧（0.5.16）

- `_settings_snapshot` 逐库 try/except：坏库跳过并回滚半截追加的数据，坏库名回 `read_errors`；`_aggregate_stats` 同理回 `stats_errors`，全坏库返回零值。
- `_maintenance_errors` 进快照与 `status`，启动 `report_status` 一并带出；面板状态卡加 `ui.status.read_errors` / `ui.status.maintenance_errors` 两条警告（两 locale 各 195 键）。
- `secure_delete` 口径：README/guide 改“释放的数据库页擦拭为零”，补 WAL、导出 JSON、`*.migrated` 备份、磁盘快照的明文边界；删除路径不变（实测未复现 FTS 词条残留）。
- 版本 0.5.16，测试数 222 项（221 passed + 1 skipped）/ 216 个 `def test_`。

## 33. A1358 复核轻项（0.5.17）

- 0 行口径：`recent_turns` 改 `max(0, int(limit))`，`engine.export` 不再把 `working_window=0` 兜成 8，`_settings_snapshot` 在窗口 0 时给空工作记忆；面板、导出、快照三处一致。
- 文案与实现对齐：`delete_memory` / `delete_working_turn` 坏库报「目标记忆库打不开（库名），未删除」，删掉「已跳过；其余库没有」的旧说法。
- 命中行定位：`working_memory_hits` 的 `character` 改传入参值，不再读 `get_owner()`（owner 空且非默认库时会指错库），顺带省一次查询。
- 并发遍历：`_settings_snapshot`、`_aggregate_stats`、`_unloaded_databases` 先 `list(...)` 再遍历，规避 `dictionary changed size during iteration`。
- 回滚完整性：快照保存/回滚 `orphan_truncated` / `archived_truncated`；维护错误提示改「最近一次维护失败…总开关关闭期间不会重试」。
- 夹具与旧话：`BadStore` 四个普通方法各自抛 `RuntimeError`；`workmem_hits` docstring、`_connect` secure_delete 注释、README 保留行数、`ui.strategy.toggle_label` 一并清理。
- 版本 0.5.17，测试数 229 项（228 passed + 1 skipped）/ 223 个 `def test_`。
