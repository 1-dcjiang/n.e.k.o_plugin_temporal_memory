import {
  Alert,
  Button,
  Card,
  Field,
  Inline,
  Input,
  Page,
  Stack,
  StatusBadge,
  Text,
  useConfirm,
} from "@neko/plugin-ui"
import type { PluginSurfaceProps } from "@neko/plugin-ui"

type MemoryConfig = {
  enabled: boolean
  working_window: number
  anchor_threshold: number
  max_anchor_summary_chars: number
  hot_recall_window_days: number
  cold_after_days: number
  dormancy_event_days: number
  dormancy_preference_days: number
  max_recall_anchors: number
  max_recall_semantics: number
  recall_char_budget: number
}

type MemoryItem = {
  table: "anchors" | "semantics"
  id: string
  text: string
  quote?: string
  kind?: string
  status?: string
  score?: number
  effective_score?: number
  age_days?: number
  next_sleep_days?: number | null
  pinned?: number | boolean
  ts?: number
  time_label?: string
  source_kind?: string
  source_event_id?: string
  conflict_group?: string
  tags?: string | string[]
  character?: string
}

type WorkingTurn = {
  id?: number
  ts?: number
  role?: string
  content?: string
  character?: string
}

type MemoryState = {
  config?: Partial<MemoryConfig>
  capacity?: { per_role?: number; max_engines?: number }
  memories?: MemoryItem[]
  memories_truncated?: boolean
  archived?: MemoryItem[]
  archived_truncated?: boolean
  conflicts?: MemoryItem[]
  conflicts_truncated?: boolean
  orphan_pending?: MemoryItem[]
  orphan_pending_truncated?: boolean
  working_memory?: WorkingTurn[]
  status?: {
    ready?: boolean
    legacy_ignored?: boolean
    stats?: {
      anchors?: number
      semantics?: number
      edges?: number
      working_turns?: number
      fts_mode?: string
      turn_no?: number
      tiers?: Record<string, number>
      statuses?: Record<string, number>
      max_role_used?: number
      pending_conflicts?: number
      unique_index_skipped?: string[]
    }
  }
}

const NUMERIC_FIELDS: Array<{
  key: keyof MemoryConfig
  label: string
  help: string
}> = [
  { key: "working_window", label: "工作记忆轮数", help: "保留最近多少轮原文；只影响本插件私有库。" },
  { key: "anchor_threshold", label: "建锚点阈值 (0~1)", help: "越高越稀疏，只留大事。" },
  { key: "max_anchor_summary_chars", label: "摘要字符上限", help: "摘要可以截断，原话证据不受这个上限影响。" },
  { key: "hot_recall_window_days", label: "hot 观察窗（天）", help: "多久内被召回算热点。" },
  { key: "cold_after_days", label: "cold 冷却期（天）", help: "多久没被召回降为冷层。" },
  { key: "dormancy_event_days", label: "事件自动休眠（天）", help: "事件超过这么多天没被确认，自动休眠；固定条目不参与。" },
  { key: "dormancy_preference_days", label: "偏好/事实自动休眠（天）", help: "偏好、事实、决定超过这么多天没被确认，自动休眠；固定条目不参与。" },
  { key: "max_recall_anchors", label: "召回锚点上限", help: "单次召回最多带几个锚点及其邻接。" },
  { key: "max_recall_semantics", label: "召回语义上限", help: "单次召回最多带几条偏好/事实。" },
  { key: "recall_char_budget", label: "召回字符预算", help: "按需召回拼给模型的字符硬上限。" },
]

const STATUS_LABEL: Record<string, string> = {
  active: "有效",
  dormant: "休眠",
  pending: "待确认",
  archived: "已移除",
}

const KIND_LABEL: Record<string, string> = {
  event: "事件",
  preference: "偏好",
  decision: "决定",
  fact: "事实",
}

function statusTone(status?: string): "success" | "warning" | "danger" {
  if (status === "active") return "success"
  if (status === "pending") return "danger"
  return "warning"
}

function shortTime(item: MemoryItem): string {
  if (item.time_label) return item.time_label
  if (!item.ts) return ""
  return new Date(item.ts * 1000).toLocaleString()
}

export default function TemporalMemorySettings(
  props: PluginSurfaceProps<MemoryState>,
) {
  const config = props.state?.config || {}
  const capacity = props.state?.capacity || {}
  const stats = props.state?.status?.stats || {}
  const tiers = stats.tiers || {}
  const statuses = stats.statuses || {}
  const memories = props.state?.memories || []
  const memoriesTruncated = Boolean(props.state?.memories_truncated)
  const archived = props.state?.archived || []
  const archivedTruncated = Boolean(props.state?.archived_truncated)
  const conflicts = props.state?.conflicts || []
  const conflictsTruncated = Boolean(props.state?.conflicts_truncated)
  const orphanPending = props.state?.orphan_pending || []
  const orphanPendingTruncated = Boolean(props.state?.orphan_pending_truncated)
  const workingMemory = props.state?.working_memory || []

  const [enabled, setEnabled] = props.useLocalState(
    "enabled",
    () => config.enabled ?? true,
  )
  const numericDefaults = Object.fromEntries(
    NUMERIC_FIELDS.map((field) => [field.key, String(config[field.key] ?? "")]),
  ) as Record<string, string>
  const [values, setValues] = props.useLocalState("values", () => numericDefaults)
  const [editId, setEditId] = props.useLocalState("editId", () => "")
  const [editText, setEditText] = props.useLocalState("editText", () => "")
  const [editKind, setEditKind] = props.useLocalState("editKind", () => "")
  const [editTags, setEditTags] = props.useLocalState("editTags", () => "")
  const [openId, setOpenId] = props.useLocalState("openId", () => "")
  const confirm = useConfirm()

  function setValue(key: string, value: string) {
    setValues({ ...values, [key]: value })
  }

  async function save() {
    const payload: Record<string, unknown> = { enabled }
    for (const field of NUMERIC_FIELDS) {
      const raw = (values[field.key] ?? "").trim()
      if (raw === "") continue
      const parsed = Number(raw)
      if (!Number.isFinite(parsed)) {
        throw new Error(`${field.label} 需要是数字`)
      }
      payload[field.key] = parsed
    }
    await props.api.call("update_settings", { config: payload })
    await props.api.refresh()
  }

  async function act(action: string, args: Record<string, unknown>) {
    await props.api.call(action, args)
    await props.api.refresh()
  }

  function startCorrection(item: MemoryItem) {
    setEditId(`${item.table}:${item.id}`)
    setEditText(item.text || "")
    setEditKind(item.kind || "fact")
    setEditTags(Array.isArray(item.tags) ? item.tags.join(",") : (item.tags || ""))
  }

  async function saveCorrection(item: MemoryItem) {
    await act("correct_memory", {
      table: item.table,
      id: item.id,
      text: editText,
      kind: editKind,
      tags: editTags,
    })
    setEditId("")
  }

  async function exportJson() {
    const result: any = await props.api.call("export_memories", {})
    const payload = result?.value ?? result?.data ?? result
    const text = JSON.stringify(payload, null, 2)
    const blob = new Blob([text], { type: "application/json" })
    const url = URL.createObjectURL(blob)
    const link = document.createElement("a")
    link.href = url
    link.download = `temporal_memory-${new Date().toISOString().slice(0, 10)}.json`
    link.click()
    URL.revokeObjectURL(url)
  }

  async function purgeOld() {
    const accepted = await confirm({
      title: "清理旧记忆",
      message:
        "将永久删除所有已加载角色库中已移除超过 30 天或距最后一次确认超过 365 天的未固定记忆。此操作不可撤销，建议先导出 JSON。",
      tone: "danger",
      confirmLabel: "永久删除",
      cancelLabel: "取消",
    })
    if (!accepted) return
    await act("purge_memories", {})
  }

  async function deleteForever(item: MemoryItem) {
    const accepted = await confirm({
      title: "永久删除这条记忆",
      message:
        "会立即删除这条记忆、关联和撤销快照，不受 30 天清理等待限制。此操作不可撤销，建议先导出 JSON。",
      tone: "danger",
      confirmLabel: "永久删除",
      cancelLabel: "取消",
    })
    if (!accepted) return
    const payload: Record<string, unknown> = {
      table: item.table,
      id: item.id,
      confirmed: true,
    }
    if (item.character !== undefined) payload.character = item.character
    await act("delete_memory", payload)
  }

  async function clearAll() {
    const first = await confirm({
      title: "清空全部记忆",
      message:
        "将永久删除所有角色库的记忆、工作记忆、关联和撤销快照。此操作不可撤销。",
      tone: "danger",
      confirmLabel: "继续",
      cancelLabel: "取消",
    })
    if (!first) return
    const second = await confirm({
      title: "最后确认",
      message: "清空后无法恢复，也没有撤销记录。确定要继续吗？",
      tone: "danger",
      confirmLabel: "清空全部",
      cancelLabel: "取消",
    })
    if (!second) return
    await act("clear_memories", { confirmed: true })
  }

  const visible = memories.filter(
    (item) => item.status !== "archived" && item.status !== "pending",
  ).slice(0, 240)
  const conflictGroups = Array.from(
    new Set(conflicts.map((item) => `${item.character ?? ""}\u0000${item.conflict_group ?? ""}`)),
  )

  return (
    <Page title="时序记忆" subtitle="原话证据 + 到期休眠 + 矛盾待确认，不自动注入对话">
      <Stack>
        <Card title="记忆状态">
          <Stack>
            {props.state?.status?.legacy_ignored && (
              <Alert tone="warning">
                检测到旧的 memory.db 与角色库并存：当前只挂载角色库，旧库没有载入，也不会出现在下面列表里。
              </Alert>
            )}
            {(props.state?.status?.stats?.unique_index_skipped?.length ?? 0) > 0 && (
              <Alert tone="warning">
                旧库存在重复事件行，唯一索引未能建立；新写入仍会前置查重，但建议导出后清理重复条目。
              </Alert>
            )}
            <Inline align="center" justify="space-between">
              <Text>运行状态</Text>
              <StatusBadge
                tone={props.state?.status?.ready ? "success" : "warning"}
                label={props.state?.status?.ready ? "运行中" : "未就绪"}
              />
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>锚点 / 偏好事实 / 关联边</Text>
              <Text>{stats.anchors ?? 0} / {stats.semantics ?? 0} / {stats.edges ?? 0}</Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>有效 / 休眠 / 待确认 / 已移除</Text>
              <Text>
                {statuses.active ?? 0} / {statuses.dormant ?? 0} / {statuses.pending ?? 0} / {statuses.archived ?? 0}
              </Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>单库最高占用（有效 + 待确认）</Text>
              <Text>{stats.max_role_used ?? 0} / {capacity.per_role ?? 5000}</Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>hot / warm / cold</Text>
              <Text>{tiers.hot ?? 0} / {tiers.warm ?? 0} / {tiers.cold ?? 0}</Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>待处理矛盾组</Text>
              <Text>{stats.pending_conflicts ?? 0}</Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>检索模式</Text>
              <Text>{stats.fts_mode || "unknown"}</Text>
            </Inline>
          </Stack>
        </Card>

        <Card title="召回方式">
          <Stack>
            <Alert tone="info">
              本插件不调用 push_message，不会主动把记忆推送到对话。
              只有用户明确问到过去、模型调用召回工具时，才返回有效条目正文；面板预览只为查看，不计入召回。
            </Alert>
          </Stack>
        </Card>

        {conflictGroups.length > 0 && (
          <Card title="待确认矛盾">
            <Stack>
              <Alert tone="warning">
                新旧说法冲突时两边都会暂停召回。选一条采用，另一条归档；选错可撤销。
              </Alert>
              {conflictsTruncated && (
                <Alert tone="warning">
                  冲突组超过 120 组，这里只显示前 120 组；处理后会继续露出后面的组。
                </Alert>
              )}
              {conflictGroups.map((key) => {
                const first = conflicts.find(
                  (item) => `${item.character ?? ""}\u0000${item.conflict_group ?? ""}` === key,
                )
                if (!first) return null
                return (
                  <Stack key={key}>
                    <Text>{`冲突组 ${first.conflict_group}${first.character ? `｜${first.character}` : ""}`}</Text>
                    {conflicts
                      .filter((item) => `${item.character ?? ""}\u0000${item.conflict_group ?? ""}` === key)
                      .map((item) => (
                        <Card key={item.id} title={`${KIND_LABEL[item.kind || ""] || item.kind}｜${shortTime(item)}`}>
                          <Stack>
                            <Text>{item.text}</Text>
                            <Text>{`原话：${item.quote || "（缺失）"}`}</Text>
                            <Inline align="center" justify="space-between">
                              <StatusBadge tone={statusTone(item.status)} label="待确认" />
                              <Button
                                tone="primary"
                                onClick={() => act("resolve_conflict", {
                                  group: first.conflict_group,
                                  keep_id: item.id,
                                  character: first.character ?? "",
                                })}
                              >
                                采用这条
                              </Button>
                            </Inline>
                          </Stack>
                        </Card>
                      ))}
                  </Stack>
                )
              })}
            </Stack>
          </Card>
        )}

        {orphanPending.length > 0 && (
          <Card title="未分组待确认">            <Stack>
              <Alert tone="warning">
                这些条目没有冲突组，但仍占用容量且不会正常召回。保留会转为有效记忆，归档会移出召回。
              </Alert>
              {orphanPendingTruncated && (
                <Alert tone="warning">
                  未分组条目超过 120 条，这里只显示最早的 120 条；处理后会露出后面的条目。
                </Alert>
              )}
              {orphanPending.map((item) => (
                <Card key={`${item.table}:${item.id}`} title={`${KIND_LABEL[item.kind || ""] || item.kind}｜${shortTime(item)}`}>
                  <Stack>
                    <Text>{item.text}</Text>
                    <Text>{`原话：${item.quote || "（缺失）"}`}</Text>
                    <Inline align="center" justify="space-between">
                      <StatusBadge tone={statusTone(item.status)} label="待确认" />
                      <Inline>
                        <Button
                          tone="primary"
                          onClick={() => act("resolve_orphan", { table: item.table, id: item.id, keep: true })}
                        >
                          保留这条
                        </Button>
                        <Button
                          tone="danger"
                          onClick={() => act("resolve_orphan", { table: item.table, id: item.id, keep: false })}
                        >
                          归档这条
                        </Button>
                      </Inline>
                    </Inline>
                  </Stack>
                </Card>
              ))}
            </Stack>
          </Card>
        )}

        {workingMemory.length > 0 && (
          <Card title="工作记忆（最近对话原文）">
            <Stack>
              <Alert tone="info">
                这里显示每个角色最近 8 轮原文，包含用户和助手的话；内容会以明文保存在插件私有 sqlite，也会出现在导出 JSON 里。
                原话由模型每轮传入，插件无法逐字核对宿主聊天记录，发现可疑内容请永久删除。
              </Alert>
              {workingMemory.slice(0, 40).map((turn, index) => (
                <Card
                  key={`${turn.character || ""}:${turn.id ?? index}`}
                  title={`${(turn.role === "assistant" || turn.role === "助手") ? "助手" : "用户"}｜${turn.ts ? new Date(turn.ts * 1000).toLocaleString() : ""}${turn.character ? `｜${turn.character}` : ""}`}
                >
                  <Text>{turn.content || ""}</Text>
                </Card>
              ))}
            </Stack>
          </Card>
        )}

        <Card title="记忆与证据">
          <Stack>
            <Alert tone="info">
              每条都能看到原话、来源和剩余休眠天数。纠正/固定/移除都会留撤销记录。
            </Alert>
            {memoriesTruncated && (
              <Alert tone="warning">
                固定条目优先，其余按时间显示，这里最多显示 240 条。固定条目不会被自动清理；取消固定或处理已显示条目后，更早的条目会继续出现在列表里。
              </Alert>
            )}
            {visible.length === 0 && <Text>还没有记忆。开启写入并聊几轮后会出现在这里。</Text>}
            {visible.map((item) => {
              const key = `${item.table}:${item.id}`
              return (
                <Card key={key} title={`${KIND_LABEL[item.kind || ""] || item.kind || "记忆"}｜${shortTime(item)}`}>
                  <Stack>
                    <Inline align="center" justify="space-between">
                      <Text>{item.text}</Text>
                      <StatusBadge
                        tone={statusTone(item.status)}
                        label={STATUS_LABEL[item.status || ""] || item.status || ""}
                      />
                    </Inline>
                    <Inline align="center" justify="space-between">
                      <Text>{`分数 ${item.effective_score ?? item.score ?? 0}｜${item.pinned ? "已固定，不衰减" : `约 ${item.next_sleep_days ?? "?"} 天后休眠`}`}</Text>
                      <Text>{item.character ? `角色 ${item.character}` : ""}</Text>
                    </Inline>
                    <Button
                      tone="default"
                      onClick={() => { setOpenId(openId === key ? "" : key) }}
                    >
                      {openId === key ? "收起原话" : "查看原话证据"}
                    </Button>
                    {openId === key && (
                      <Stack>
                        <Text>{`原话：${item.quote || "（缺失，不会进入召回）"}`}</Text>
                        <Text>{`来源：${item.source_kind || "未知"}${item.source_event_id ? `｜事件 ${item.source_event_id}` : ""}`}</Text>
                        <Button tone="danger" onClick={() => deleteForever(item)}>
                          永久删除这条（不可撤销）
                        </Button>
                      </Stack>
                    )}
                    <Inline align="center" justify="space-between">
                      <Button
                        tone="success"
                        onClick={() => act("confirm_memory", { table: item.table, id: item.id })}
                      >
                        确认有效
                      </Button>
                      <Button
                        tone="default"
                        onClick={() => act("pin_memory", {
                          table: item.table,
                          id: item.id,
                          pinned: !item.pinned,
                        })}
                      >
                        {item.pinned ? "取消固定" : "手动保留"}
                      </Button>
                      <Button tone="default" onClick={() => { startCorrection(item) }}>
                        纠正
                      </Button>
                      <Button
                        tone="danger"
                        onClick={() => act("archive_memory", { table: item.table, id: item.id })}
                      >
                        移除
                      </Button>
                    </Inline>
                    {editId === key && (
                      <Stack>
                        <Field label="摘要/内容">
                          <Input value={editText} onChange={(value: string) => { setEditText(value) }} />
                        </Field>
                        <Field label="原话证据（只读）">
                          <Text>{item.quote || "（缺失）"}</Text>
                        </Field>
                        <Field label="类型">
                          <Inline align="center" justify="space-between">
                            {Object.entries(KIND_LABEL).map(([kind, label]) => (
                              <Button
                                key={kind}
                                tone={editKind === kind ? "primary" : "default"}
                                onClick={() => { setEditKind(kind) }}
                              >
                                {label}
                              </Button>
                            ))}
                          </Inline>
                        </Field>
                        <Field label="标签（逗号分隔）">
                          <Input value={editTags} onChange={(value: string) => { setEditTags(value) }} />
                        </Field>
                        <Inline align="center" justify="space-between">
                          <Button tone="primary" onClick={() => saveCorrection(item)}>保存纠正</Button>
                          <Button tone="default" onClick={() => { setEditId("") }}>取消</Button>
                        </Inline>
                      </Stack>
                    )}
                  </Stack>
                </Card>
              )
            })}
            <Alert tone="warning">
              导出文件是明文 JSON，包含全部记忆与原话证据；只保存在可信位置，不要上传到公开网盘或聊天群。
            </Alert>
            <Inline align="center" justify="space-between">
              <Button tone="default" onClick={() => act("undo_last", {})}>撤销上一步</Button>
              <Button tone="default" onClick={exportJson}>导出 JSON</Button>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>容量吃紧时可清理长期已移除/休眠的旧条目。</Text>
              <Button tone="default" onClick={purgeOld}>清理旧记忆</Button>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>彻底清空所有角色库，立即生效且不可撤销。</Text>
              <Button tone="danger" onClick={clearAll}>清空全部记忆</Button>
            </Inline>
          </Stack>
        </Card>

        {archived.length > 0 && (
          <Card title="已移除的记忆（可永久删除）">
            <Stack>
              <Alert tone="info">
                这些条目已移出召回，但原话仍以明文保存在插件 sqlite 里，平时不计入召回，也不占容量。
                想立刻抹掉请点「永久删除这条」；也可以等「清理旧记忆」的 30 天期限。
              </Alert>
              {archivedTruncated && (
                <Alert tone="warning">
                  已移除条目超过 120 条，这里只显示最近的 120 条；删除后会露出更早的条目。
                </Alert>
              )}
              {archived.map((item) => (
                <Card key={`${item.character || ""}:${item.table}:${item.id}`} title={`${KIND_LABEL[item.kind || ""] || item.kind || "记忆"}｜${shortTime(item)}`}>
                  <Stack>
                    <Text>{item.text}</Text>
                    <Text>{`原话：${item.quote || "（缺失）"}`}</Text>
                    <Inline align="center" justify="space-between">
                      <Text>{item.character ? `角色 ${item.character}` : "默认库"}</Text>
                      <Button tone="danger" onClick={() => deleteForever(item)}>
                        永久删除这条（不可撤销）
                      </Button>
                    </Inline>
                  </Stack>
                </Card>
              ))}
            </Stack>
          </Card>
        )}

        <Card title="写入与生命周期">
          <Stack>
            <Alert tone="info">
              关闭总开关后不再写入新记忆，也不再接受模型召回；已有记忆仍保留在面板里，可纠正、移除或永久删除。
            </Alert>
            <Field label="启用记忆写入">
              <Button tone={enabled ? "default" : "primary"} onClick={() => { setEnabled(!enabled) }}>
                {enabled ? "已启用，点击停用" : "已停用，点击启用"}
              </Button>
            </Field>
            {NUMERIC_FIELDS.map((field) => (
              <Field key={field.key} label={field.label} help={field.help}>
                <Input value={values[field.key] ?? ""} onChange={(value: string) => setValue(field.key, value)} />
              </Field>
            ))}
          </Stack>
        </Card>

        <Card title="保存">
          <Stack>
            <Text>改动只写回本插件配置，不影响宿主或其他插件。</Text>
            <Button tone="primary" onClick={save}>保存设置</Button>
          </Stack>
        </Card>
      </Stack>
    </Page>
  )
}
