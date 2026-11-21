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
  preview?: string
  character?: string
}

type UnloadedDatabase = {
  path: string
  name?: string
  character?: string
  anchors?: number
  semantics?: number
  pending?: number
  conflict_groups?: number
  working_turns?: number
  last_activity?: number
  ok?: boolean
  error?: string
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
  unloaded_databases?: UnloadedDatabase[]
  pending_alert_days?: number
  pending_overdue?: number
  pending_oldest_days?: number
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

// 文案一律走 i18n：这里只存 key，渲染时用 props.t 取当前语言。
const NUMERIC_FIELDS: Array<{
  key: keyof MemoryConfig
  label: string
  help: string
}> = [
  { key: "working_window", label: "ui.field.working_window.label", help: "ui.field.working_window.help" },
  { key: "anchor_threshold", label: "ui.field.anchor_threshold.label", help: "ui.field.anchor_threshold.help" },
  { key: "max_anchor_summary_chars", label: "ui.field.max_anchor_summary_chars.label", help: "ui.field.max_anchor_summary_chars.help" },
  { key: "hot_recall_window_days", label: "ui.field.hot_recall_window_days.label", help: "ui.field.hot_recall_window_days.help" },
  { key: "cold_after_days", label: "ui.field.cold_after_days.label", help: "ui.field.cold_after_days.help" },
  { key: "dormancy_event_days", label: "ui.field.dormancy_event_days.label", help: "ui.field.dormancy_event_days.help" },
  { key: "dormancy_preference_days", label: "ui.field.dormancy_preference_days.label", help: "ui.field.dormancy_preference_days.help" },
  { key: "max_recall_anchors", label: "ui.field.max_recall_anchors.label", help: "ui.field.max_recall_anchors.help" },
  { key: "max_recall_semantics", label: "ui.field.max_recall_semantics.label", help: "ui.field.max_recall_semantics.help" },
  { key: "recall_char_budget", label: "ui.field.recall_char_budget.label", help: "ui.field.recall_char_budget.help" },
]

const STATUS_LABEL_KEY: Record<string, string> = {  active: "ui.status.active",
  dormant: "ui.status.dormant",
  pending: "ui.status.pending",
  archived: "ui.status.archived",
}

const KIND_LABEL_KEY: Record<string, string> = {
  event: "ui.kind.event",
  preference: "ui.kind.preference",
  decision: "ui.kind.decision",
  fact: "ui.kind.fact",
}

// 助手角色在历史数据里既可能是英文 role，也可能是中文值；用转义避免面板出现硬编码中文。
const ASSISTANT_ROLE = "assistant"
const ASSISTANT_ROLE_LEGACY = "\u52a9\u624b"

function isAssistantRole(role?: string): boolean {
  return role === ASSISTANT_ROLE || role === ASSISTANT_ROLE_LEGACY
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
  const t = (key: string, params?: Record<string, any>): string =>
    props.t ? props.t(key, params) : key
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
  const unloaded = props.state?.unloaded_databases || []
  const pendingAlertDays = Number(props.state?.pending_alert_days ?? 7) || 7
  const pendingOverdue = Number(props.state?.pending_overdue ?? 0) || 0
  const pendingOldestDays = Number(props.state?.pending_oldest_days ?? 0) || 0
  const workingWindow = Number(config.working_window ?? 8) || 8

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
  const [notice, setNotice] = props.useLocalState("notice", () => "")
  const confirm = useConfirm()

  function setValue(key: string, value: string) {
    setValues({ ...values, [key]: value })
  }

  function workingHitLines(hits: WorkingTurn[]): string {
    // 只给要删的那几行原文预览：文本匹配可能命中相近的行，删前让人自己看一眼
    return hits
      .slice(0, 8)
      .map((hit) => t("ui.confirm.working_hits_line", {
        role: isAssistantRole(hit.role) ? t("ui.working.role_assistant") : t("ui.working.role_user"),
        time: hit.ts ? new Date(hit.ts * 1000).toLocaleString() : "",
        text: (hit.preview || hit.content || "").slice(0, 60),
      }))
      .join("\n")
  }

  async function save() {
    const payload: Record<string, unknown> = { enabled }
    for (const field of NUMERIC_FIELDS) {
      const raw = (values[field.key] ?? "").trim()
      if (raw === "") continue
      const parsed = Number(raw)
      if (!Number.isFinite(parsed)) {
        throw new Error(t("ui.errors.numeric_field", { field: t(field.label) }))
      }
      payload[field.key] = parsed
    }
    await props.api.call("update_settings", { config: payload })
    await props.api.refresh()
  }

  async function act(
    action: string,
    args: Record<string, unknown>,
    label = action,
  ): Promise<any> {
    let payload: any = null
    try {
      const result: any = await props.api.call(action, args)
      payload = result?.value ?? result?.data ?? result
    } catch (error) {
      setNotice(t("ui.notice.action_failed", {
        label,
        message: error instanceof Error ? error.message : String(error),
      }))
      return null
    }
    if (payload && payload.ok === false) {
      // 清空/清理是跨库操作：单个库失败时后端返回 ok=false + failed 计数
      const failed = Number(payload.failed ?? 0)
      setNotice(t("ui.notice.action_partial", {
        label,
        detail: failed > 0 ? t("ui.notice.action_partial_detail", { failed }) : "",
      }))
    } else {
      setNotice("")
    }
    await props.api.refresh()
    return payload
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
      title: t("ui.confirm.purge_title"),
      message: t("ui.confirm.purge_message"),
      tone: "danger",
      confirmLabel: t("ui.common.delete_forever"),
      cancelLabel: t("ui.common.cancel"),
    })
    if (!accepted) return
    const result = await act("purge_memories", {}, t("ui.action.purge"))
    if (result?.ok) {
      setNotice(t("ui.notice.purge_done", {
        total: result.total ?? 0,
        anchors: result.anchors ?? 0,
        semantics: result.semantics ?? 0,
      }))
    }
  }

  async function deleteWorkingTurn(turn: WorkingTurn) {
    const accepted = await confirm({
      title: t("ui.confirm.working_title"),
      message: t("ui.confirm.working_message"),
      tone: "danger",
      confirmLabel: t("ui.common.delete_forever"),
      cancelLabel: t("ui.common.cancel"),
    })
    if (!accepted) return
    await act(
      "delete_working_turn",
      {
        id: turn.id ?? 0,
        character: turn.character ?? "",
        confirmed: true,
      },
      t("ui.action.delete_working_turn"),
    )
  }

  async function deleteForever(item: MemoryItem) {
    const accepted = await confirm({
      title: t("ui.confirm.memory_title"),
      message: t("ui.confirm.memory_message"),
      tone: "danger",
      confirmLabel: t("ui.common.delete_forever"),
      cancelLabel: t("ui.common.cancel"),
    })
    if (!accepted) return
    const payload: Record<string, unknown> = {
      table: item.table,
      id: item.id,
      confirmed: true,
    }
    if (item.character !== undefined) payload.character = item.character
    const result = await act("delete_memory", payload, t("ui.action.delete_forever"))
    const hits = (result?.working_memory_hits ?? []) as WorkingTurn[]
    if (!hits || hits.length === 0) return
    const alsoDelete = await confirm({
      title: t("ui.confirm.working_hits_title"),
      message: `${t("ui.confirm.working_hits_message", { count: hits.length })}\n\n${workingHitLines(hits)}`,
      tone: "danger",
      confirmLabel: t("ui.confirm.delete_together"),
      cancelLabel: t("ui.confirm.keep_for_now"),
    })
    if (!alsoDelete) {
      setNotice(t("ui.notice.memory_deleted_hits", { count: hits.length }))
      return
    }
    let removed = 0
    for (const hit of hits) {
      try {
        await props.api.call("delete_working_turn", {
          id: hit.id ?? 0,
          character: hit.character ?? "",
          confirmed: true,
        })
        removed += 1
      } catch {
        // 单行删失败不影响其余行；最后统一提示成功数
      }
    }
    setNotice(t("ui.notice.memory_deleted_with_hits", { removed, total: hits.length }))
    await props.api.refresh()
  }

  async function clearAll() {
    const first = await confirm({
      title: t("ui.confirm.clear_title"),
      message: t("ui.confirm.clear_message"),
      tone: "danger",
      confirmLabel: t("ui.common.continue"),
      cancelLabel: t("ui.common.cancel"),
    })
    if (!first) return
    const second = await confirm({
      title: t("ui.confirm.clear_final_title"),
      message: t("ui.confirm.clear_final_message"),
      tone: "danger",
      confirmLabel: t("ui.common.clear_all"),
      cancelLabel: t("ui.common.cancel"),
    })
    if (!second) return
    const result = await act("clear_memories", { confirmed: true }, t("ui.action.clear_all"))
    if (result?.ok) {
      const reclaimed =
        result.reclaimed === false ? t("ui.notice.clear_not_reclaimed") : ""
      setNotice(t("ui.notice.clear_done", {
        databases: result.databases ?? 0,
        memories: (result.anchors ?? 0) + (result.semantics ?? 0),
        turns: result.working_turns ?? 0,
        detail: reclaimed,
      }))
    }
  }

  const visible = memories.filter(
    (item) => item.status !== "archived" && item.status !== "pending",
  ).slice(0, 240)
  const conflictGroups = Array.from(
    new Set(conflicts.map((item) => `${item.character ?? ""}\u0000${item.conflict_group ?? ""}`)),
  )

  return (
    <Page title={t("ui.title")} subtitle={t("ui.subtitle")}>
      <Stack>
        {notice && (
          <Alert tone="warning">{notice}</Alert>
        )}
        <Card title={t("ui.status.title")}>
          <Stack>
            {props.state?.status?.legacy_ignored && (
              <Alert tone="warning">
                {t("ui.status.legacy_ignored")}
              </Alert>
            )}
            {(props.state?.status?.stats?.unique_index_skipped?.length ?? 0) > 0 && (
              <Alert tone="warning">
                {t("ui.status.unique_index_skipped")}
              </Alert>
            )}
            <Inline align="center" justify="space-between">
              <Text>{t("ui.status.running_label")}</Text>
              <StatusBadge
                tone={props.state?.status?.ready ? "success" : "warning"}
                label={props.state?.status?.ready ? t("ui.status.running") : t("ui.status.not_ready")}
              />
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>{t("ui.status.counts_label")}</Text>
              <Text>{stats.anchors ?? 0} / {stats.semantics ?? 0} / {stats.edges ?? 0}</Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>{t("ui.status.state_counts_label")}</Text>
              <Text>
                {statuses.active ?? 0} / {statuses.dormant ?? 0} / {statuses.pending ?? 0} / {statuses.archived ?? 0}
              </Text>
            </Inline>
            {(statuses.pending ?? 0) > 0 && (
              <Alert tone="warning">
                {pendingOverdue > 0
                  ? t("ui.status.pending_overdue", {
                    overdue: pendingOverdue,
                    days: pendingAlertDays,
                    oldest: pendingOldestDays,
                  })
                  : t("ui.status.pending_notice", { count: statuses.pending ?? 0 })}
              </Alert>
            )}
            <Inline align="center" justify="space-between">
              <Text>{t("ui.status.occupancy_label")}</Text>
              <Text>{stats.max_role_used ?? 0} / {capacity.per_role ?? 5000}</Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>{t("ui.status.tiers")}</Text>
              <Text>{tiers.hot ?? 0} / {tiers.warm ?? 0} / {tiers.cold ?? 0}</Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>{t("ui.status.conflict_groups_label")}</Text>
              <Text>{stats.pending_conflicts ?? 0}</Text>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>{t("ui.status.fts_label")}</Text>
              <Text>{stats.fts_mode || "unknown"}</Text>
            </Inline>
          </Stack>
        </Card>

        <Card title={t("ui.recall.title")}>
          <Stack>
            <Alert tone="info">
              {t("ui.recall.hint")}
            </Alert>
          </Stack>
        </Card>

        {unloaded.length > 0 && (
          <Card title={t("ui.unloaded.title")}>
            <Stack>
              <Alert tone="info">
                {t("ui.unloaded.hint", { count: unloaded.length })}
              </Alert>
              {unloaded.map((db) => (
                <Card
                  key={db.path}
                  title={`${db.character || t("ui.unloaded.no_character")} | ${db.name || ""}`}
                >
                  <Stack>
                    <Text>
                      {t("ui.unloaded.counts", {
                        anchors: db.anchors ?? 0,
                        semantics: db.semantics ?? 0,
                        pending: db.pending ?? 0,
                        groups: db.conflict_groups ?? 0,
                        turns: db.working_turns ?? 0,
                      })}
                    </Text>
                    <Text>
                      {db.ok === false
                        ? t("ui.unloaded.read_failed", { error: db.error || t("ui.common.unknown_reason") })
                        : db.last_activity
                          ? t("ui.unloaded.last_activity", { time: new Date(db.last_activity * 1000).toLocaleString() })
                          : t("ui.unloaded.empty")}
                    </Text>
                    <Inline align="center" justify="space-between">
                      <Text>{t("ui.unloaded.file", { name: db.name || "" })}</Text>
                      {db.ok !== false && (
                        <Button
                          tone="default"
                          onClick={() => act("attach_database", { path: db.path }, t("ui.action.attach_database"))}
                        >
                          {t("ui.unloaded.attach")}
                        </Button>
                      )}
                    </Inline>
                  </Stack>
                </Card>
              ))}
            </Stack>
          </Card>
        )}

        {conflictGroups.length > 0 && (
          <Card title={t("ui.conflict.title")}>
            <Stack>
              <Alert tone="warning">
                {t("ui.conflict.hint")}
              </Alert>
              {conflictsTruncated && (
                <Alert tone="warning">
                  {t("ui.conflict.truncated")}
                </Alert>
              )}
              {conflictGroups.map((key) => {
                const first = conflicts.find(
                  (item) => `${item.character ?? ""}\u0000${item.conflict_group ?? ""}` === key,
                )
                if (!first) return null
                return (
                  <Stack key={key}>
                    <Text>{t("ui.conflict.group_label", {
                      group: first.conflict_group ?? "",
                      character: first.character ? t("ui.conflict.group_character", { character: first.character }) : "",
                    })}</Text>
                    {conflicts
                      .filter((item) => `${item.character ?? ""}\u0000${item.conflict_group ?? ""}` === key)
                      .map((item) => (
                        <Card
                          key={item.id}
                          title={`${t(KIND_LABEL_KEY[item.kind || ""] || "ui.common.memory")} | ${shortTime(item)}`}
                        >
                          <Stack>
                            <Text>{item.text}</Text>
                            <Text>{t("ui.memory.quote_label", { quote: item.quote || t("ui.common.missing") })}</Text>
                            <Inline align="center" justify="space-between">
                              <StatusBadge tone={statusTone(item.status)} label={t("ui.status.pending")} />
                              <Button
                                tone="primary"
                                onClick={() => act("resolve_conflict", {
                                  group: first.conflict_group,
                                  keep_id: item.id,
                                  character: first.character ?? "",
                                })}
                              >
                                {t("ui.memory.adopt")}
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
          <Card title={t("ui.orphan.title")}>
            <Stack>
              <Alert tone="warning">
                {t("ui.orphan.hint")}
              </Alert>
              {orphanPendingTruncated && (
                <Alert tone="warning">
                  {t("ui.orphan.truncated")}
                </Alert>
              )}
              {orphanPending.map((item) => (
                <Card
                  key={`${item.table}:${item.id}`}
                  title={`${t(KIND_LABEL_KEY[item.kind || ""] || "ui.common.memory")} | ${shortTime(item)}`}
                >
                  <Stack>
                    <Text>{item.text}</Text>
                    <Text>{t("ui.memory.quote_label", { quote: item.quote || t("ui.common.missing") })}</Text>
                    <Inline align="center" justify="space-between">
                      <StatusBadge tone={statusTone(item.status)} label={t("ui.status.pending")} />
                      <Inline>
                        <Button
                          tone="primary"
                          onClick={() => act("resolve_orphan", { table: item.table, id: item.id, keep: true })}
                        >
                          {t("ui.orphan.keep")}
                        </Button>
                        <Button
                          tone="danger"
                          onClick={() => act("resolve_orphan", { table: item.table, id: item.id, keep: false })}
                        >
                          {t("ui.orphan.archive")}
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
          <Card title={t("ui.working.title")}>
            <Stack>
              <Alert tone="info">
                {t("ui.working.hint", {
                  window: workingWindow,
                  turns: Math.ceil(workingWindow / 2),
                })}
              </Alert>
              {workingMemory.map((turn, index) => (
                <Card
                  key={`${turn.character || ""}:${turn.id ?? index}`}
                  title={`${isAssistantRole(turn.role) ? t("ui.working.role_assistant") : t("ui.working.role_user")} | ${turn.ts ? new Date(turn.ts * 1000).toLocaleString() : ""}${turn.character ? ` | ${turn.character}` : ""}`}
                >
                  <Stack>
                    <Text>{turn.content || ""}</Text>
                    <Inline align="center" justify="end">
                      <Button tone="danger" onClick={() => deleteWorkingTurn(turn)}>
                        {t("ui.working.delete")}
                      </Button>
                    </Inline>
                  </Stack>
                </Card>
              ))}
            </Stack>
          </Card>
        )}

        <Card title={t("ui.memory.title")}>
          <Stack>
            <Alert tone="info">
              {t("ui.memory.hint")}
            </Alert>
            {memoriesTruncated && (
              <Alert tone="warning">
                {t("ui.memory.truncated")}
              </Alert>
            )}
            {visible.length === 0 && <Text>{t("ui.memory.empty")}</Text>}
            {visible.map((item) => {
              const key = `${item.table}:${item.id}`
              return (
                <Card
                  key={key}
                  title={`${t(KIND_LABEL_KEY[item.kind || ""] || "ui.common.memory")} | ${shortTime(item)}`}
                >
                  <Stack>
                    <Inline align="center" justify="space-between">
                      <Text>{item.text}</Text>
                      <StatusBadge
                        tone={statusTone(item.status)}
                        label={t(STATUS_LABEL_KEY[item.status || ""] || "ui.common.unknown")}
                      />
                    </Inline>
                    <Inline align="center" justify="space-between">
                      <Text>
                        {item.pinned
                          ? t("ui.memory.pinned_score", { score: item.effective_score ?? item.score ?? 0 })
                          : t("ui.memory.sleeping_score", {
                            score: item.effective_score ?? item.score ?? 0,
                            days: item.next_sleep_days ?? "?",
                          })}
                      </Text>
                      <Text>{item.character ? t("ui.memory.character", { character: item.character }) : ""}</Text>
                    </Inline>
                    <Button
                      tone="default"
                      onClick={() => { setOpenId(openId === key ? "" : key) }}
                    >
                      {openId === key ? t("ui.memory.hide_quote") : t("ui.memory.quote")}
                    </Button>
                    {openId === key && (
                      <Stack>
                        <Text>{t("ui.memory.quote_label", {
                          quote: item.quote || t("ui.memory.quote_missing_no_recall"),
                        })}</Text>
                        <Text>{t("ui.memory.source", {
                          kind: item.source_kind || t("ui.common.unknown"),
                          event: item.source_event_id
                            ? t("ui.memory.source_event", { id: item.source_event_id })
                            : "",
                        })}</Text>
                        <Button tone="danger" onClick={() => deleteForever(item)}>
                          {t("ui.memory.delete_forever")}
                        </Button>
                      </Stack>
                    )}
                    <Inline align="center" justify="space-between">
                      <Button
                        tone="success"
                        onClick={() => act("confirm_memory", { table: item.table, id: item.id })}
                      >
                        {t("ui.memory.confirm")}
                      </Button>
                      <Button
                        tone="default"
                        onClick={() => act("pin_memory", {
                          table: item.table,
                          id: item.id,
                          pinned: !item.pinned,
                        })}
                      >
                        {item.pinned ? t("ui.memory.unpin") : t("ui.memory.pin")}
                      </Button>
                      <Button tone="default" onClick={() => { startCorrection(item) }}>
                        {t("ui.memory.correct")}
                      </Button>
                      <Button
                        tone="danger"
                        onClick={() => act("archive_memory", { table: item.table, id: item.id })}
                      >
                        {t("ui.memory.remove")}
                      </Button>
                    </Inline>
                    {editId === key && (
                      <Stack>
                        <Field label={t("ui.memory.text_field")}>
                          <Input value={editText} onChange={(value: string) => { setEditText(value) }} />
                        </Field>
                        <Field label={t("ui.memory.quote_field")}>
                          <Text>{item.quote || t("ui.common.missing")}</Text>
                        </Field>
                        <Field label={t("ui.memory.kind_field")}>
                          <Inline align="center" justify="space-between">
                            {Object.entries(KIND_LABEL_KEY).map(([kind, labelKey]) => (
                              <Button
                                key={kind}
                                tone={editKind === kind ? "primary" : "default"}
                                onClick={() => { setEditKind(kind) }}
                              >
                                {t(labelKey)}
                              </Button>
                            ))}
                          </Inline>
                        </Field>
                        <Field label={t("ui.memory.tags_field")}>
                          <Input value={editTags} onChange={(value: string) => { setEditTags(value) }} />
                        </Field>
                        <Inline align="center" justify="space-between">
                          <Button tone="primary" onClick={() => saveCorrection(item)}>{t("ui.memory.save_correction")}</Button>
                          <Button tone="default" onClick={() => { setEditId("") }}>{t("ui.common.cancel")}</Button>
                        </Inline>
                      </Stack>
                    )}
                  </Stack>
                </Card>
              )
            })}
            <Alert tone="warning">
              {t("ui.memory.export_warning")}
            </Alert>
            <Inline align="center" justify="space-between">
              <Button tone="default" onClick={() => act("undo_last", {})}>{t("ui.memory.undo")}</Button>
              <Button tone="default" onClick={exportJson}>{t("ui.memory.export")}</Button>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>{t("ui.memory.purge_hint")}</Text>
              <Button tone="default" onClick={purgeOld}>{t("ui.memory.purge")}</Button>
            </Inline>
            <Inline align="center" justify="space-between">
              <Text>{t("ui.memory.clear_hint")}</Text>
              <Button tone="danger" onClick={clearAll}>{t("ui.memory.clear")}</Button>
            </Inline>
          </Stack>
        </Card>

        {archived.length > 0 && (
          <Card title={t("ui.archived.title")}>
            <Stack>
              <Alert tone="info">
                {t("ui.archived.hint")}
              </Alert>
              {archivedTruncated && (
                <Alert tone="warning">
                  {t("ui.archived.truncated")}
                </Alert>
              )}
              {archived.map((item) => (
                <Card
                  key={`${item.character || ""}:${item.table}:${item.id}`}
                  title={`${t(KIND_LABEL_KEY[item.kind || ""] || "ui.common.memory")} | ${shortTime(item)}`}
                >
                  <Stack>
                    <Text>{item.text}</Text>
                    <Text>{t("ui.memory.quote_label", { quote: item.quote || t("ui.common.missing") })}</Text>
                    <Inline align="center" justify="space-between">
                      <Text>{item.character ? t("ui.memory.character", { character: item.character }) : t("ui.archived.default_db")}</Text>
                      <Button tone="danger" onClick={() => deleteForever(item)}>
                        {t("ui.memory.delete_forever")}
                      </Button>
                    </Inline>
                  </Stack>
                </Card>
              ))}
            </Stack>
          </Card>
        )}

        <Card title={t("ui.strategy.title")}>
          <Stack>
            <Alert tone="info">
              {t("ui.strategy.disabled_hint")}
            </Alert>
            <Field label={t("ui.strategy.toggle_label")}>
              <Button tone={enabled ? "default" : "primary"} onClick={() => { setEnabled(!enabled) }}>
                {enabled ? t("ui.strategy.enabled_on") : t("ui.strategy.enabled_off")}
              </Button>
            </Field>
            {NUMERIC_FIELDS.map((field) => (
              <Field key={field.key} label={t(field.label)} help={t(field.help)}>
                <Input value={values[field.key] ?? ""} onChange={(value: string) => setValue(field.key, value)} />
              </Field>
            ))}
          </Stack>
        </Card>

        <Card title={t("ui.save.title")}>
          <Stack>
            <Text>{t("ui.save.hint")}</Text>
            <Button tone="primary" onClick={save}>{t("ui.save.button")}</Button>
          </Stack>
        </Card>
      </Stack>
    </Page>
  )
}
