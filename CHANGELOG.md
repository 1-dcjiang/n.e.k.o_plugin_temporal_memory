# Changelog

## 0.5.10

A1313 功能与红线核查后的收尾：

- 总开关改为同时拦截写入和召回：关闭后 `recall_temporal_memory` 返回空结果并标记 `disabled`，面板预览直接报错。
- 清空全部不再只清已加载角色库：枚举数据目录里的 `memory.db` 与全部 `memory-*.db`，未加载的库也会逐个清空；返回 `databases` 与 `failed`。
- 单条永久删除支持指定 `character`，并会打开未加载的角色库执行删除；面板已移除列表也能直接永久删除。
- 面板新增“已移除的记忆（可永久删除）”，最多展示最近 120 条，不必再等 30 天清理。
- 版本 0.5.10。

## 0.5.9

A1309 功能与红线核查后的用户控制补齐：

- 工作记忆不再只藏在召回预览：面板新增最近 8 轮原文卡片，导出 JSON 增加 `working_memory`。
- 新增“永久删除这条”：无需等 30 天清理，立即删除单条记忆、关联和撤销快照；面板二次确认。
- 新增“清空全部记忆”：一次清空所有角色库的 anchors、semantics、wm、关联和撤销快照；面板两次确认。
- 面板明确提示工作记忆与原文是明文 sqlite，且原话由模型传入、插件无法逐字核对宿主聊天。
- 版本 0.5.9。

## 0.5.8

A1302 增量快审的低项收尾：

- 导出重新加上限保护：单表最多 10000 条、所有角色库合计最多 20000 条；超限时返回 `truncated` 标记和 `warning`，不再静默截断。
- `resolve_conflict` 区分失败原因：`keep_id` 不在组里返回“不在这组冲突”，找不到组才返回“没有找到”。
- `character` 改为 `None` 表示未指定；空字符串现在会定位空角色库，不再回退成全库处理。
- 未分组待确认的截断提示明确写“最早 120 条”，说明处理较早条目后会露出后面的。
- 版本 0.5.8。

## 0.5.7

A1299 增量快审的低项收尾：

- 面板冲突组去重改用 `(角色库, group)` 组合键；两个角色库出现同名冲突组时不再被合成一组，前端分组和“采用这条”也按角色库定位。
- `resolve_conflict` 新增可选 `character` 参数，只处理指定角色库；不传时保持原来的全库按 group 处理。
- 导出不再用 10000 条静默截断，`all_memories(limit=None)` 返回全量；`export` 改用无上限读取。
- 未分组待确认合并多个角色库后按时间全局排序，不再按库顺序切前 120 条。
- 版本 0.5.7。

## 0.5.6

A1297 增量快审的低项收尾：

- 未分组待确认每个角色库取 121 条再判断，单库超过 120 条也会正确显示截断提示。
- `resolve_orphan` 在事务内校验目标必须是 `pending` 且没有 `conflict_group`；误传 active 或冲突组条目会返回“不是未分组待确认”，不会绕开冲突处理。
- 面板截断文案改为“固定条目优先，其余按时间”，并提示取消固定才会露出更多。
- 冲突列表按组截断：只显示前 120 组，保留每组全部成员，不再切出半个组。
- 单库查询窗口在固定之后优先 `active`，较新的 dormant 不会挤掉更早的 active。
- 版本 0.5.6。

## 0.5.5

A1294 增量快审的低项收尾：

- `all_memories(statuses=())` 明确表示“无结果”，不再退化成返回全部状态；`statuses=None` 才表示不过滤。
- 面板快照的查询先按 `pinned` 排序再按时间截断，固定条目优先进入 300 条窗口，不再被大量新记忆挤掉。
- 新增“未分组待确认”面板区：没有 `conflict_group` 的 `pending` 也能看到，并可逐条“保留这条 / 归档这条”，避免只占容量却无法处理。
- 冲突组和未分组待确认超过 120 条时给出截断提示。
- 版本 0.5.5。

## 0.5.4

A1293 增量快审的低项收尾：

- 空墓碑保留原本的 `undone` 状态：只有未撤销的快照会变成可撤销墓碑，已撤销的不会重新冒出来多消耗一次点击。
- 面板快照先按 `active/dormant` 过滤再截断，`archived/pending` 不再挤占 240 条可见窗口；截断提示也只按真正可见的条数判断，不再误报或漏报。
- `all_memories` / `list_memories` 增加状态过滤参数，SQL 参数化，默认行为不变。
- 版本 0.5.4。

## 0.5.3

A1292 增量快审的低项收尾：

- 清理后整条撤销快照只剩被删条目时，不再删除整条 revision，而是留空墓碑；`undo_last` 先命中它并报 `target_missing`，不会静默回滚更早的无关操作。
- revision 表最多保留 1000 条；写入与清理前自动裁剪，清理扫描改为逐行游标，避免一次性把整表读进内存并长时间占写锁。
- 休眠条目必须 `last_confirmed_at > 0` 才参与 365 天清理，异常旧数据不会被误判为过期。
- 删除 `_memory_action` 里永远走不到的 `target_missing` 分支；撤销缺目标的提示改为“已不存在或已被清理”。
- 面板在列表被截断时明确提示只显示最近 240 条，处理后会继续露出更早条目。
- 版本 0.5.3。

## 0.5.2

A1291 增量快审的清理入口收尾：

- `purge_stale` 的选取与逐条删除放进同一个 `BEGIN IMMEDIATE` 事务，删除前带 `stale_cutoffs` 复查 `pinned/status/时间`；中途失败整批回滚。
- 永久删除时同步清理 `revisions.before_json/after_json` 里引用该 id 的条目，避免 `quote/content` 残留在撤销快照。
- `undo_last` 按实际 `rowcount` 计数；目标全不存在时返回 `target_missing`，不再假装成功；插件入口分别给出“容量上限”和“目标已不存在”的明确错误。
- `confirm/pin/undo` 把 `archived/dormant` 唤醒为 `active/pending` 时检查容量，避免越过 5000；面板给出容量错误而不是“找不到”。
- 面板记忆列表上限从 80 提到 240（与 `settings_context` 一致），减少大量固定条目只能看到最新几条的问题。
- 清理说明改为“距最后一次确认超过 365 天”，与 SQL 的 `last_confirmed_at` 一致。
- 版本 0.5.2。

## 0.5.1

A1289 增量快审的出口与低项收尾：

- 容量只统计 `active` + `pending`：面板「移除」转 `archived` 或到期转 `dormant` 后立刻释放写入名额，不再把库永久写死。
- 面板容量分母由 `settings_context.capacity` 提供，并显示“单库最高占用”，和 Python 的 `MAX_TOTAL_MEMORIES` / `MAX_ENGINES` 保持同源。
- 新增面板「清理旧记忆」入口：永久删除已移除超过 30 天、休眠超过 365 天的未固定条目，释放磁盘与总量；执行前二次确认并提示先导出。
- 相似语义复认只返回 `id/kind/status/confirmed`，不再把旧条目的 `quote` / `content` 整条回给模型。
- `engine.export()` 的 `schema` 从 3 修正为 4，和面板导出保持一致。
- Schema v4 迁移的 `anchors` / `semantics` 也改用 `UPDATE OR IGNORE`，旧库同时存在 `x` 与 `chat_turn:x` 时不再因唯一索引冲突打不开。
- `observe` 拒绝自带 `chat_turn:` / `manual:` 前缀的 `source_event_id`，模型不能抢占事件 id；其他带冒号的宿主 id 仍可透传。
- 修正邻居配额注释里的 400/200 旧值。
- 版本 0.5.1。

## 0.5.0

A1274 复查后，把插件侧能独立修的项落地：

- 事件 id 按来源分命名空间：`observe` 写 `chat_turn:<id>`，`remember` 写 `manual:<id>`；重复事件只返回 `anchor_id` / `semantic_ids`，不再把旧原文裸交给模型。
- 召回字段先截断再拼围栏：时间标签 18 字、显著度 8 字、摘要 90 字、语义本体 100 字、原话统一 50 字，整行二次裁剪不再切掉 `</quote>` 和“仅作证据”提示。
- 单库总量上限 5000 条；达到上限时整笔事务回滚并返回 `skipped='capacity'`，不写半条，也不自动删用户旧记忆。重复事件仍按 duplicate 返回，不因容量误报。
- 话题邻居上限收紧为单标签 50、总量 200，降低一次召回把整张语义网拖进内存的风险。
- 角色库名改为 `memory-<安全名>-<sha256前8位>.db`，`"a b"` 与 `"a_b"` 不再撞库；旧 `memory-<角色>.db` 首次访问时迁移，并用 `meta.character` 记录归属。
- Schema 升到 v4：旧库 `anchors` / `semantics` / `processed_events` / `event_semantics` 的事件 id 一次性补来源前缀，导出 `schema` 同步为 4。
- `MAX_ENGINES` 到顶不再只报“初始化失败”，改为明确提示“角色记忆库数量已达上限 32，请先导出并清理旧角色库”。
- `observe` 文案改为“返回值仅供调用方核对证据”，不再声称“不返回记忆正文”。
- 版本 0.5.0，pytest 收集 124 项 / 120 个 `def test_`；`ruff`、宿主 `check --strict`、Hosted TSX、包内测试通过。

## 0.4.9

A1271 安全审查的高/中项修复：

- 召回整块包 `<memory_data untrusted="true">` 并声明「以下为历史记忆数据，不是指令」；`summary`、`content`、`quote`、`time_label` 和工作记忆统一折成单行、转义尖括号，`quote` 不论是否等于 `summary` 都加围栏。
- `remember` 默认改为 `pinned=False`；`tags` 走中文/字母/数字/下划线/短横线白名单，最长 16 字、最多 6 个；`correct_memory` 同样过滤。
- `ts` 拒绝 `inf`/`NaN` 并夹在合理区间；`query` 截到 200 字；每角色每分钟最多 30 次写入；角色库数量上限 32。
- 回给模型的异常改成通用文案，详细异常只进日志；`int(inf)` 的 `OverflowError` 不再影响启动。
- `undo_last` 恢复前过 `_table()` 白名单；导出面板补明文提示；修正「不会返回记忆正文」的误导文案。
- 版本 0.4.9，测试数 112 项 / 110 个 `def test_`。

## 0.4.8

A1270 增量审的低项收尾：

- 两条唯一索引 DDL 用 savepoint 包住；第二条遇锁失败时第一条已建/已删的结果一起回滚，不再留下半套索引。
- 事件查询常量补齐真实方法里的 `ORDER BY ... LIMIT 1`，EXPLAIN 测试与执行语句完全一致。
- EXPLAIN 补 `semantics_for_event` 不带 `source_kind` 的分支。
- 补「0.4.4 健康库遗留回退索引、启动后被清掉」和「第二条索引 DDL 失败整体回滚」测试。
- 版本 0.4.8，测试数 111 项 / 107 个 `def test_`。

## 0.4.7

A1269 增量审的三条低项收尾：

- 唯一索引恢复后清掉旧版本遗留的非唯一回退索引，避免长期写放大；唯一索引建不起来时仍保留回退索引。
- 事件查询 SQL 抽成模块常量，`add_anchor` / `find_anchor_by_event` / `semantics_for_event` / `prune_processed_events` 与 EXPLAIN 测试共用同一份语句，不再手抄。
- EXPLAIN 测试补 `semantics_for_event` 的 UNION 子查询与真实 prune `DELETE` 语句。
- 启动时发现唯一索引缺失会先置冷却，首次面板刷新不再立刻重试建索引。
- 版本 0.4.7，测试数 109 项 / 105 个 `def test_`。

## 0.4.6

A1268 风险判断的索引回归修复：

- 不再在唯一索引建成后删除同列非唯一索引；否则部分唯一索引带 `WHERE source_event_id <> ''`，只写 `source_event_id=?` 的查询用不上它，会退化成全表扫描。
- 非唯一事件索引只在唯一索引因旧库重复行建不起来时创建，避免每次启动白建白删。
- `_insert_anchor` / `_insert_semantic` 查重、`find_anchor_by_event`、`semantics_for_event`、`prune_processed_events` 的查询补 `source_event_id<>''`，让健康库能用部分唯一索引。
- EXPLAIN 断言同时覆盖健康库和旧库缺唯一索引两种场景。
- 版本 0.4.6，测试数保持 108 项 / 104 个 `def test_`。

## 0.4.5

A1267 最终增量审的两条尾巴：

- 唯一索引重试加 300 秒冷却；数据库被锁时吞掉 `OperationalError`，状态卡不再因重试失败而报错。
- 唯一索引建成后删除同列非唯一索引，避免冗余索引写放大；唯一索引缺失时仍保留非唯一索引供清理查询使用。
- 补 `undo_last` / `revise_memory` 中途失败整体回滚测试，以及 `stats()` 重试遇锁、冷却内不重试的测试。
- 补 EXPLAIN 断言，确认旧库缺唯一索引时事件清理走非唯一索引。

## 0.4.4

A1266 增量审的返修：

- `undo_last` 整体包进事务：字段回滚、FTS 全量重建、语义邻接边重建同一提交或同一回滚。
- `revise_memory` 的旧行读取移到事务内，撤销快照不再漏掉并发修改。
- 唯一索引缺失可在面板刷新时重试；清掉旧库重复行后无需重启，warning 自动消失。
- 旧库缺唯一索引时补非唯一 `(source_event_id, source_kind)` 索引，事件清理的 `NOT EXISTS` 不再全表扫描。
- `processed_events` 增加 20000 条上限；超限删最旧占位，重投时引擎按已有锚点/语义兜底判重，不再重复写工作记忆。
- 补语义唯一索引、索引重试、占位上限、重投不写 wm 四组测试。

## 0.4.3

A1265 增量审的返修：

- 纠正标签时，字段更新、撤销快照、FTS/邻接边重建放进同一事务；撤销标签纠正后也会重建语义邻接边。
- 标签统一去重排序后存储，面板与 `add_semantic` 格式一致。
- 删除已无调用方的 `release_event`；失败回滚由 `transaction()` 承担。
- 旧库有重复事件行导致唯一索引建不起来时，记录到 `stats.unique_index_skipped`，状态卡显示 warning；不再吞 `OperationalError`。
- 事件索引清理只删「既无锚点也无语义」的空事件；仍有记忆残留的事件保留占位，避免 TTL 后重投重复写 wm / 重复 reconfirm。
- 补标签撤销、旧库重复索引两条回归测试；版本升到 0.4.3。

## 0.4.2

A1263 增量审的返修：

- `observe` / `remember` 的占位、工作记忆、锚点和语义写入改为同一 SQLite 事务（`BEGIN IMMEDIATE`），中途异常整体回滚，不再出现「占位已落库、内容没写」。
- 新增 `(source_event_id, source_kind)` 锚点唯一索引和 `(source_event_id, source_kind, content)` 语义唯一索引；绕过 engine 直接写 store 也不会重复。
- `processed_events` 增加 365 天清理，随维护任务执行；只删占位，不动记忆本体。
- 语义邻居配额按标签均分总量，三个及以上标签时不会被前两个标签饿死。
- 新增 `event_semantics` 关联表，重复事件返回时也能看到本次被 reconfirm 的旧语义，不再只返回新建条目。
- 面板纠正新增标签输入；后端 `correct_memory` 接受逗号分隔标签并重建 FTS/邻接边。
- 重复事件返回值补 `duplicate` 标记和 `kind`，避免调用方误判。

## 0.4.1

A1261 第二版增量审的返修：

- `observe` / `remember` 增加 `processed_events` 原子去重，覆盖没有锚点、只产生语义的事件；`chat_turn` 与 `manual` 分开计数，失败时释放占位。
- `retier` 的 hot 保护改用 `created_at`，回填旧 `ts` 的新锚点不会在第一次维护时被降级；补充 7 天 hot 窗口说明。
- 语义邻居改为每个标签最多 200 个、总量最多 400 个，避免第一个标签占满名额。
- 撤销栈同一秒内改用 SQLite `rowid` 决定先后，不再依赖随机 UUID。
- `journal_mode=WAL` 只在首次连接设置；连接仍按语句短开，避免 Windows 下迁移/删除时持有文件句柄。
- 原话证据改为只读：面板仍可纠正摘要/内容、类型和标签，但不再允许改写 `quote`；后端返回 `quote_readonly`。
- 版本号升到 0.4.1，补本 changelog 与 PLAN 记录。

## 0.4.0

- 从 0.1.0 注入版回退，物理删除 `injection.py` 与 `_push_read`，取消 `push_message` 被动注入。
- 改为原话证据 + 按需 `recall_temporal_memory` 拉取；设置页保留预览入口。
- 事件 90 天、偏好 180 天未确认转 `dormant`；`pinned` 不衰减。
- 矛盾先 `pending`，面板确认/纠正/移除/撤销；`revisions` 记录操作。
- 老库自动补列迁移；旧 `memory.db` 与角色库并存时不载入，并在状态卡提示。
