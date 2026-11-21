"""N.E.K.O 插件入口：证据记忆 + 按需召回。

v0.4 取消原版 `push_message` 被动注入：写入只进插件私有库，不再往对话里塞任何
内容；模型只有在用户明确问到过去时，才通过 `recall_temporal_memory` 主动拉取。
"""

from __future__ import annotations

import asyncio
import hashlib
import os
import re
import shutil
import sqlite3
import time
from typing import Any

from plugin.sdk.plugin import (
    Err,
    NekoPluginBase,
    Ok,
    SdkError,
    lifecycle,
    llm_tool,
    neko_plugin,
    plugin_entry,
    timer_interval,
    ui,
)

from .engine import DEFAULT_SETTINGS, MAX_EXPORT_ROWS_PER_TABLE, MemoryEngine
from .memory_store import MAX_TOTAL_MEMORIES

PLUGIN_DIR = os.path.dirname(os.path.abspath(__file__))
MAX_TURN_CHARS = 4000
MAX_QUERY_CHARS = 200
MAX_ENGINES = 32
MAX_EXPORT_TOTAL_ROWS = 20000
MAX_TAGS = 6
TAG_RE = re.compile(r'[0-9A-Za-z_\-\u4e00-\u9fff]{1,16}')
_TS_FLOOR = 10 * 365 * 86400
WRITE_LIMIT = 30
WRITE_WINDOW_SECONDS = 60


def _clamp_ts(value: Any) -> int | None:
    """模型传入的 ts 不可信：拒绝 inf/NaN，并夹在当前时间前后合理区间。"""
    if value is None:
        return None
    try:
        ts = int(value)
    except (TypeError, ValueError, OverflowError):
        return None
    now = int(time.time())
    return max(now - _TS_FLOOR, min(ts, now + 86400))


def _sanitize_tags(values: Any) -> list[str]:
    """标签统一走白名单：中文/字母/数字/下划线/短横线，最长 16 字。"""
    if isinstance(values, str):
        values = values.split(',')
    tags: list[str] = []
    for raw in values or []:
        tag = str(raw).strip()
        if tag and len(tag) <= 16 and TAG_RE.fullmatch(tag) and tag not in tags:
            tags.append(tag)
        if len(tags) >= MAX_TAGS:
            break
    return tags

SETTING_RANGES: dict[str, tuple[float, float]] = {
    'working_window': (0, 50),
    'anchor_threshold': (0.0, 1.0),
    'max_anchor_summary_chars': (20, 1000),
    'hot_recall_window_days': (1, 365),
    'cold_after_days': (1, 3650),
    'dormancy_event_days': (1, 3650),
    'dormancy_preference_days': (1, 3650),
    'max_recall_anchors': (1, 30),
    'max_recall_semantics': (1, 60),
    'recall_char_budget': (200, 8000),
}


@neko_plugin
class TemporalMemoryPlugin(NekoPluginBase):
    """分层时序记忆：原话证据、久不确认休眠、矛盾待确认、固定不衰减。"""

    def __init__(self, ctx):
        super().__init__(ctx)
        self._engines: dict[str, MemoryEngine] = {}
        self._settings: dict[str, Any] = dict(DEFAULT_SETTINGS)
        self._ready = False
        self._legacy_ignored = False
        self._init_lock = asyncio.Lock()
        self._write_times: dict[str, list[float]] = {}
        self._engine_errors: dict[str, str] = {}

    # ------------------------------------------------------------------ 生命周期

    @lifecycle(id="startup")
    async def on_startup(self, **_):
        await self._load_settings()
        # 只加载已存在的老库，不主动创建空 memory.db；首个带角色名的调用会按角色建库。
        legacy = self._db_path()
        legacy_ignored = os.path.exists(legacy) and bool(self._character_db_paths())
        self._legacy_ignored = legacy_ignored
        if os.path.exists(legacy) and not legacy_ignored:
            await self._ensure_engine()
        if legacy_ignored:
            self.logger.warning('检测到 memory.db 与角色库并存，已跳过老库加载：%s', legacy)
            for path in self._character_db_paths():
                owner = self._db_owner(path)
                if owner:
                    await self._ensure_engine(owner)
                else:
                    name = os.path.basename(path)[len('memory-'):-len('.db')]
                    # 没有 owner 的旧库名不带哈希；新库若没写 owner 就不自动猜角色。
                    if name and not re.search(r'-[0-9a-f]{8}$', name):
                        await self._ensure_engine(name)
        for engine in self._engines.values():
            await asyncio.to_thread(engine.maintenance)
        self._ready = True
        self.report_status({'ready': True, 'stats': self._aggregate_stats(),
                            'legacy_ignored': legacy_ignored})
        return Ok({'status': 'ready', 'legacy_ignored': legacy_ignored})

    @lifecycle(id="shutdown")
    async def on_shutdown(self, **_):
        self._ready = False
        self._legacy_ignored = False
        self._engines = {}
        return Ok({'status': 'shutdown'})

    @lifecycle(id="config_change")
    async def on_config_change(self, **_):
        await self._load_settings()
        for engine in self._engines.values():
            engine.settings = dict(self._settings)
        return Ok({'status': 'config_updated'})

    # ------------------------------------------------------------------ 写入入口

    @plugin_entry(
        id="observe",
        name="记录一轮对话",
        description=(
            "把一轮用户/助手对话写进本插件私有库，并从用户原话里摘出偏好、事实与事件。"
            "只落库，不主动推送，也不会把记忆塞进下一轮对话；返回值仅供调用方核对证据。"
            "⚠️ 每轮对话结束后调用一次；user_text 必须是原始用户发言，不要改写。"
        ),
        input_schema={
            'type': 'object',
            'properties': {
                'user_text': {'type': 'string', 'description': '用户这一轮的原始发言'},
                'assistant_text': {'type': 'string', 'description': '助手这一轮的回复（可选）'},
                'source_event_id': {'type': 'string', 'description': '宿主事件 id（可选，用于回查原话）'},
                'ts': {'type': 'integer', 'description': 'Unix 秒级时间戳，缺省取当前时间'},
            },
            'required': ['user_text'],
        },
        llm_result_fields=['anchored', 'anchor', 'semantics'],
    )
    async def observe(self, user_text: str = '', assistant_text: str = '',
                      source_event_id: str = '', ts: int | None = None, **_):
        if not self._settings.get('enabled', True):
            # 总开关关闭时直接短路，避免为了返回 skipped 给该角色建空库
            return Ok({'ok': True, 'skipped': 'disabled', 'turn_no': 0,
                       'anchored': False, 'anchor': None, 'semantics': []})
        character = self._character(_)
        engine = await self._ensure_engine(character)
        if engine is None:
            return Err(SdkError(self._engine_error(character)))
        user_text = (user_text or '').strip()[:MAX_TURN_CHARS]
        if not user_text:
            return Err(SdkError('user_text 不能为空：请传入这一轮用户的原始发言'))
        raw_source_id = str(source_event_id or '')[:120]
        if raw_source_id.startswith(('chat_turn:', 'manual:')):
            return Err(SdkError(
                'source_event_id 只接受宿主原始事件 id，不要自带 chat_turn: / manual: 前缀'))
        if not self._allow_write(character):
            return Err(SdkError('写入过于频繁，请稍后再试'))
        assistant_text = (assistant_text or '')[:MAX_TURN_CHARS]
        try:
            result = await asyncio.to_thread(
                engine.observe, user_text, assistant_text, _clamp_ts(ts),
                source_event_id=raw_source_id)
        except Exception as exc:
            self.logger.warning('写入记忆失败: %s', exc)
            return Err(SdkError('写入记忆失败，请稍后重试'))
        return Ok(result)

    @plugin_entry(
        id="remember",
        name="记住一件事",
        description=(
            "用户明确要求「记住这件事／别忘」时调用，直接写入一条带原话证据的记忆。"
            "pinned=true 表示用户要求长期保留，不参与自动休眠。"
        ),
        input_schema={
            'type': 'object',
            'properties': {
                'text': {'type': 'string', 'description': '要记住的内容，必须保留用户原话'},
                'source_event_id': {'type': 'string', 'description': '宿主事件 id（可选）'},
                'ts': {'type': 'integer', 'description': '事件发生时间（Unix 秒），缺省为当前时间'},
                'pinned': {'type': 'boolean', 'description': '是否长期保留，默认 false；只有用户明确要求长期保留才传 true'},
                'tags': {
                    'type': 'array',
                    'items': {'type': 'string'},
                    'description': '可选标签，每个限中文/字母/数字/下划线/短横线，最长 16 字，最多 6 个',
                },
            },
            'required': ['text'],
        },
        llm_result_fields=['anchor', 'kind'],
    )
    async def remember(self, text: str = '', source_event_id: str = '',
                       ts: int | None = None, pinned: bool = False,
                       tags: list[str] | None = None, **_):
        if not self._settings.get('enabled', True):
            # 同上：关闭时不建空库，只回报跳过
            return Ok({'ok': True, 'skipped': 'disabled', 'anchor': None,
                       'kind': '', 'semantics': []})
        character = self._character(_)
        engine = await self._ensure_engine(character)
        if engine is None:
            return Err(SdkError(self._engine_error(character)))
        text = (text or '').strip()[:MAX_TURN_CHARS]
        if not text:
            return Err(SdkError('text 不能为空：请传入需要记住的原话'))
        if not self._allow_write(character):
            return Err(SdkError('写入过于频繁，请稍后再试'))
        safe_tags = _sanitize_tags(tags)
        try:
            result = await asyncio.to_thread(
                engine.remember, text, _clamp_ts(ts), safe_tags,
                pinned=bool(pinned), source_event_id=str(source_event_id or '')[:120])
        except Exception as exc:
            self.logger.warning('写入锚点失败: %s', exc)
            return Err(SdkError('写入记忆失败，请稍后重试'))
        return Ok(result)

    # ------------------------------------------------------------------ 读取入口

    @llm_tool(
        name='recall_temporal_memory',
        description=(
            '按需检索长期时序记忆。仅当用户提到「上次」「之前」「你还记得吗」，'
            '或当前问题确实需要旧偏好、旧决定、跨天上下文时调用。'
            '返回 active 条目及其原话证据；休眠、待确认冲突和已移除的条目不会返回。'
            '不要逐字念给用户听。'
        ),
        parameters={
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': '当前话题的关键词或一句话'},
            },
            'required': ['query'],
        },
    )
    async def recall_tool(self, query: str = '', **_):
        character = self._character(_)
        if not self._settings.get('enabled', True):
            return {'text': '', 'chars': 0, 'anchors': 0, 'semantics': 0,
                    'disabled': True, 'reason': 'memory_disabled'}
        engine = await self._ensure_engine(character)
        query = (query or '').strip()[:MAX_QUERY_CHARS]
        if engine is None:
            return {'text': '', 'chars': 0, 'anchors': 0, 'semantics': 0,
                    'error': self._engine_error(character)}
        if not query:
            return {'text': '', 'chars': 0, 'anchors': 0, 'semantics': 0}
        try:
            result = await asyncio.to_thread(engine.recall, query)
        except Exception as exc:
            self.logger.warning('召回失败，返回空结果: %s', exc)
            return {'text': '', 'chars': 0, 'anchors': 0, 'semantics': 0}
        return {
            'text': result.get('prompt_block', ''),
            'chars': result.get('chars', 0),
            'anchors': len(result.get('anchors') or []),
            'semantics': len(result.get('semantics') or []),
        }

    @plugin_entry(
        id="recall",
        name="预览召回",
        description="只给设置页做召回预览；agent_hidden，不向模型暴露。",
        input_schema={
            'type': 'object',
            'properties': {
                'query': {'type': 'string', 'description': '当前问题里的关键词或一句话'},
            },
            'required': ['query'],
        },
        metadata={'agent_hidden': True},
        llm_result_fields=['prompt_block', 'chars'],
    )
    async def recall(self, query: str = '', **_):
        character = self._character(_)
        if not self._settings.get('enabled', True):
            return Err(SdkError('记忆总开关已关闭：写入和召回都已停用'))
        engine = await self._ensure_engine(character)
        if engine is None:
            return Err(SdkError(self._engine_error(character)))
        query = (query or '').strip()[:MAX_QUERY_CHARS]
        if not query:
            return Err(SdkError('query 不能为空：请传入当前问题里的关键词'))
        try:
            result = await asyncio.to_thread(
                engine.recall, query, count_as_recall=False)
        except Exception as exc:
            self.logger.warning('召回预览失败: %s', exc)
            return Err(SdkError('召回失败，请稍后重试'))
        return Ok(result)

    @plugin_entry(
        id="timeline",
        name="读取时间线",
        description="设置页按时间段查看锚点；agent_hidden，不向模型暴露。",
        input_schema={
            'type': 'object',
            'properties': {
                'start_ts': {'type': 'integer', 'description': '起始时间（Unix 秒），可省略表示从头'},
                'end_ts': {'type': 'integer', 'description': '结束时间（Unix 秒），缺省为现在'},
                'limit': {'type': 'integer', 'description': '最多返回多少个锚点，默认 30'},
            },
        },
        metadata={'agent_hidden': True},
        llm_result_fields=['count', 'text'],
    )
    async def timeline(self, start_ts: int | None = None,
                       end_ts: int | None = None, limit: int = 30, **_):
        character = self._character(_)
        engine = await self._ensure_engine(character)
        if engine is None:
            return Err(SdkError(self._engine_error(character)))
        try:
            limit = max(1, min(int(limit or 30), 200))
        except (TypeError, ValueError):
            return Err(SdkError('limit 必须是数字'))
        try:
            result = await asyncio.to_thread(engine.timeline, start_ts, end_ts, limit)
        except Exception as exc:
            self.logger.warning('读取时间线失败: %s', exc)
            return Err(SdkError('读取时间线失败，请稍后重试'))
        return Ok(result)

    # ------------------------------------------------------------------ UI

    @ui.context(id="settings")
    async def settings_context(self):
        return await asyncio.to_thread(self._settings_snapshot)

    def _settings_snapshot(self) -> dict[str, Any]:
        memories: list[dict[str, Any]] = []
        archived: list[dict[str, Any]] = []
        conflicts: list[dict[str, Any]] = []
        orphans: list[dict[str, Any]] = []
        working: list[dict[str, Any]] = []
        orphan_truncated = False
        archived_truncated = False
        for character, engine in self._engines.items():
            for item in engine.list_memories(
                    300, statuses=('active', 'dormant')):
                item['character'] = character
                memories.append(item)
            batch = engine.list_memories(121, statuses=('archived',))
            if len(batch) > 120:
                archived_truncated = True
                batch = batch[:120]
            for item in batch:
                item['character'] = character
                archived.append(item)
            for turn in engine.store.recent_turns(8):
                turn['character'] = character
                working.append(turn)
            for item in engine.store.pending_conflicts():
                item['character'] = character
                item['table'] = item.pop('table_name', item.get('table') or '')
                item['text'] = item.get('content') or ''
                conflicts.append(item)
            batch = engine.store.orphan_pending(121)
            if len(batch) > 120:
                orphan_truncated = True
                batch = batch[:120]
            for item in batch:
                item['character'] = character
                item['table'] = item.pop('table_name', item.get('table') or '')
                item['text'] = item.get('content') or ''
                orphans.append(item)
        memories.sort(key=lambda item: (item.get('pinned') not in (1, True),
                                        item.get('status') != 'active',
                                        -int(item.get('ts') or 0)))
        archived.sort(key=lambda item: -int(item.get('ts') or 0))
        visible_conflicts: list[dict[str, Any]] = []
        conflict_groups: set[tuple[str, str]] = set()
        conflict_truncated = False
        for item in conflicts:
            group = str(item.get('conflict_group') or item.get('id'))
            key = (str(item.get('character') or ''), group)
            if key not in conflict_groups:
                if len(conflict_groups) >= 120:
                    conflict_truncated = True
                    continue
                conflict_groups.add(key)
            visible_conflicts.append(item)
        orphans.sort(key=lambda item: int(item.get('ts') or 0))
        return {
            'config': dict(self._settings),
            'status': {'ready': self._ready, 'stats': self._aggregate_stats(),
                       'legacy_ignored': self._legacy_ignored},
            'capacity': {'per_role': MAX_TOTAL_MEMORIES, 'max_engines': MAX_ENGINES},
            'memories': memories[:240],
            'memories_truncated': len(memories) > 240,
            'archived': archived,
            'archived_truncated': archived_truncated,
            'working_memory': working,
            'conflicts': visible_conflicts,
            'conflicts_truncated': conflict_truncated,
            'orphan_pending': orphans[:120],
            'orphan_pending_truncated': orphan_truncated or len(orphans) > 120,
        }

    @ui.action(label='保存设置', tone='primary', refresh_context=True)
    @plugin_entry(
        id='update_settings',
        name='保存记忆设置',
        description='保存时序记忆的写入、休眠与召回参数。由设置面板的「保存设置」按钮调用。',
        input_schema={
            'type': 'object',
            'properties': {
                'config': {'type': 'object', 'description': '要更新的设置键值'},
            },
            'required': ['config'],
        },
        metadata={'agent_hidden': True},
    )
    async def update_settings(self, config: dict | None = None, **_):
        if not isinstance(config, dict):
            return Err(SdkError('config 必须是对象'))
        merged = {**DEFAULT_SETTINGS, **dict(self._settings)}
        for key, default in DEFAULT_SETTINGS.items():
            if key not in config:
                continue
            try:
                if isinstance(default, bool):
                    raw = config[key]
                    merged[key] = (raw.strip().lower() in {'1', 'true', 'yes', 'on'}
                                   if isinstance(raw, str) else bool(raw))
                else:
                    merged[key] = type(default)(config[key])
            except (TypeError, ValueError, OverflowError):
                return Err(SdkError(f'{key} 的取值无法转换为 {type(default).__name__}'))
            if key in SETTING_RANGES:
                low, high = SETTING_RANGES[key]
                try:
                    if not low <= merged[key] <= high:
                        return Err(SdkError(
                            f'{key} 超出允许范围 {low}~{high}，未保存'))
                except TypeError:
                    return Err(SdkError(f'{key} 的取值不在合法范围内'))
        self._settings = merged
        for engine in self._engines.values():
            engine.settings = dict(merged)
        try:
            await self.config.update({'settings': merged}, timeout=5.0)
        except Exception as exc:
            self.logger.warning('保存设置失败: %s', exc)
            return Err(SdkError('设置已应用但保存失败，请稍后重试'))
        return Ok({'status': 'saved', 'settings': merged})

    @ui.action(label='确认这条', refresh_context=True)
    @plugin_entry(
        id='confirm_memory',
        name='确认记忆',
        description='用户核对原话后确认有效，刷新确认时间，退出待确认/休眠状态。',
        input_schema={
            'type': 'object',
            'properties': {
                'table': {'type': 'string', 'enum': ['anchors', 'semantics']},
                'id': {'type': 'string'},
            },
            'required': ['table', 'id'],
        },
        metadata={'agent_hidden': True},
    )
    async def confirm_memory(self, table: str = '', id: str = '', **_):
        return await self._memory_action('confirm', table, id)

    @ui.action(label='固定/取消固定', refresh_context=True)
    @plugin_entry(
        id='pin_memory',
        name='手动保留',
        description='固定后不参与自动休眠与衰减；再次调用可取消。',
        input_schema={
            'type': 'object',
            'properties': {
                'table': {'type': 'string', 'enum': ['anchors', 'semantics']},
                'id': {'type': 'string'},
                'pinned': {'type': 'boolean'},
            },
            'required': ['table', 'id', 'pinned'],
        },
        metadata={'agent_hidden': True},
    )
    async def pin_memory(self, table: str = '', id: str = '', pinned: bool = True, **_):
        return await self._memory_action('pin', table, id, pinned=bool(pinned))

    @ui.action(label='保存纠正', refresh_context=True)
    @plugin_entry(
        id='correct_memory',
        name='纠正记忆',
        description='修正摘要/内容、类型或标签；原话证据只读，改动可撤销。',
        input_schema={
            'type': 'object',
            'properties': {
                'table': {'type': 'string', 'enum': ['anchors', 'semantics']},
                'id': {'type': 'string'},
                'text': {'type': 'string', 'description': '纠正后的摘要/内容'},
                'kind': {'type': 'string', 'enum': ['event', 'preference', 'decision', 'fact']},
                'tags': {'type': 'string', 'description': '逗号分隔的标签'},
            },
            'required': ['table', 'id'],
        },
        metadata={'agent_hidden': True},
    )
    async def correct_memory(self, table: str = '', id: str = '', text: str = '',
                             kind: str = '', tags: str | None = None, **_):
        fields: dict[str, Any] = {}
        if text:
            fields['summary' if table == 'anchors' else 'content'] = text
        if kind:
            fields['kind'] = kind
        if tags is not None:
            fields['tags'] = _sanitize_tags(tag.strip() for tag in tags.split(','))
        return await self._memory_action('correct', table, id, **fields)

    @ui.action(label='移除', refresh_context=True)
    @plugin_entry(
        id='archive_memory',
        name='移除记忆',
        description='从召回中移除，但保留原话与撤销记录；可通过「撤销上一步」恢复。',
        input_schema={
            'type': 'object',
            'properties': {
                'table': {'type': 'string', 'enum': ['anchors', 'semantics']},
                'id': {'type': 'string'},
            },
            'required': ['table', 'id'],
        },
        metadata={'agent_hidden': True},
    )
    async def archive_memory(self, table: str = '', id: str = '', **_):
        return await self._memory_action('archive', table, id)

    @ui.action(label='采用这条', refresh_context=True)
    @plugin_entry(
        id='resolve_conflict',
        name='处理矛盾',
        description='在待确认冲突里选一条保留，其余归档；可撤销。',
        input_schema={
            'type': 'object',
            'properties': {
                'group': {'type': 'string'},
                'keep_id': {'type': 'string'},
                'character': {'type': 'string'},
            },
            'required': ['group', 'keep_id'],
        },
        metadata={'agent_hidden': True},
    )
    async def resolve_conflict(self, group: str = '', keep_id: str = '',
                               character: str | None = None, **_):
        if not group or not keep_id:
            return Err(SdkError('group 与 keep_id 都不能为空'))
        try:
            if character is not None:
                engine = self._engines.get(character)
                if engine is None:
                    return Err(SdkError('没有找到这个角色的记忆库'))
                engines = [engine]
            else:
                engines = list(self._engines.values())
            results = []
            reasons = []
            for engine in engines:
                result = await asyncio.to_thread(engine.resolve_conflict, group, keep_id)
                if result.get('ok'):
                    results.append(result)
                else:
                    reasons.append(result.get('reason'))
            if not results:
                if 'keep_not_in_group' in reasons:
                    return Err(SdkError('keep_id 不在这组冲突里'))
                return Err(SdkError('没有找到这组冲突'))
            return Ok(results[0])
        except Exception as exc:
            self.logger.warning('处理矛盾失败: %s', exc)
            return Err(SdkError('处理矛盾失败，请稍后重试'))

    @ui.action(label='处理未分组待确认', refresh_context=True)
    @plugin_entry(
        id='resolve_orphan',
        name='处理未分组待确认',
        description='保留或归档没有冲突组的待确认条目，避免只占容量却从面板消失。',
        input_schema={
            'type': 'object',
            'properties': {
                'table': {'type': 'string', 'enum': ['anchors', 'semantics']},
                'id': {'type': 'string'},
                'keep': {'type': 'boolean'},
            },
            'required': ['table', 'id', 'keep'],
        },
        metadata={'agent_hidden': True},
    )
    async def resolve_orphan(self, table: str = '', id: str = '',
                             keep: bool = True, **_):
        if table not in {'anchors', 'semantics'} or not id:
            return Err(SdkError('table 与 id 都必填'))
        capacity_hit = False
        not_orphan = False
        try:
            for engine in self._engines.values():
                result = await asyncio.to_thread(
                    engine.resolve_orphan, table, id, bool(keep))
                if result.get('ok'):
                    return Ok(result)
                if result.get('reason') == 'capacity':
                    capacity_hit = True
                elif result.get('reason') == 'not_orphan':
                    not_orphan = True
        except Exception as exc:
            self.logger.warning('处理未分组待确认失败: %s', exc)
            return Err(SdkError('处理失败，请稍后重试'))
        if capacity_hit:
            return Err(SdkError('活跃/待确认记忆已达上限，请先移除或清理旧记忆'))
        if not_orphan:
            return Err(SdkError('这条不是未分组的待确认记忆'))
        return Err(SdkError('没有找到这条待确认记忆'))

    @ui.action(label='撤销上一步', refresh_context=True)
    @plugin_entry(
        id='undo_last',
        name='撤销上一步',
        description='撤销最近一次面板纠正、固定、移除或冲突处理。',
        input_schema={'type': 'object', 'properties': {}},
        metadata={'agent_hidden': True},
    )
    async def undo_last(self, **_):
        candidates = []
        try:
            for engine in self._engines.values():
                revision = await asyncio.to_thread(engine.store.last_revision)
                if revision:
                    candidates.append((int(revision['ts']), revision['id'], engine))
            if not candidates:
                return Err(SdkError('没有可撤销的操作'))
            _, _, engine = max(candidates, key=lambda item: (item[0], item[1]))
            result = await asyncio.to_thread(engine.undo)
        except Exception as exc:
            self.logger.warning('撤销失败: %s', exc)
            return Err(SdkError('撤销失败，请稍后重试'))
        if not result.get('ok'):
            reason = result.get('reason')
            if reason == 'capacity':
                return Err(SdkError('活跃/待确认记忆已达上限，请先移除或清理旧记忆'))
            if reason == 'target_missing':
                return Err(SdkError('目标记忆已不存在或已被清理，无法撤销'))
            return Err(SdkError('没有可撤销的操作'))
        return Ok(result)

    @ui.action(label='导出 JSON', refresh_context=False)
    @plugin_entry(
        id='export_memories',
        name='导出记忆',
        description='导出全部记忆与原话证据（JSON），只含本插件私有库。',
        input_schema={'type': 'object', 'properties': {}},
        metadata={'agent_hidden': True},
    )
    async def export_memories(self, **_):
        payload: dict[str, Any] = {'exported_at': 0, 'schema': 4,
                                   'anchors': [], 'semantics': [],
                                   'working_memory': []}
        truncated = {'anchors': False, 'semantics': False, 'total': False}
        try:
            for character, engine in self._engines.items():
                one = await asyncio.to_thread(engine.export)
                payload['exported_at'] = max(payload['exported_at'], one['exported_at'])
                one_truncated = one.get('truncated') or {}
                for table in ('anchors', 'semantics'):
                    truncated[table] = truncated[table] or bool(one_truncated.get(table))
                    remaining = MAX_EXPORT_TOTAL_ROWS - len(payload['anchors']) \
                        - len(payload['semantics'])
                    if remaining <= 0:
                        if one[table]:
                            truncated['total'] = True
                        continue
                    batch = one[table][:remaining]
                    if len(one[table]) > remaining:
                        truncated['total'] = True
                    for item in batch:
                        item['character'] = character
                        payload[table].append(item)
                for turn in one.get('working_memory') or []:
                    turn['character'] = character
                    payload['working_memory'].append(turn)
        except Exception as exc:
            self.logger.warning('导出记忆失败: %s', exc)
            return Err(SdkError('导出失败，请稍后重试'))
        payload['truncated'] = truncated
        if any(truncated.values()):
            payload['warning'] = (
                f'导出超过上限只保留部分条目（单表 {MAX_EXPORT_ROWS_PER_TABLE} 条、'
                f'合计 {MAX_EXPORT_TOTAL_ROWS} 条）；建议先清理旧记忆再导出完整数据。'
            )
        return Ok(payload)

    @ui.action(label='永久删除', tone='danger', refresh_context=True)
    @plugin_entry(
        id='delete_memory',
        name='永久删除单条记忆',
        description='立即永久删除一条记忆、关联和撤销快照；不可撤销，需面板二次确认。',
        input_schema={
            'type': 'object',
            'properties': {
                'table': {'type': 'string', 'enum': ['anchors', 'semantics']},
                'id': {'type': 'string'},
                'character': {'type': 'string', 'description': '条目所属角色库，空串表示默认库'},
                'confirmed': {'type': 'boolean'},
            },
            'required': ['table', 'id', 'confirmed'],
        },
        metadata={'agent_hidden': True},
    )
    async def delete_memory(self, table: str = '', id: str = '',
                            character: str | None = None,
                            confirmed: bool = False, **_):
        if table not in {'anchors', 'semantics'} or not id:
            return Err(SdkError('table 与 id 都必填'))
        if not confirmed:
            return Err(SdkError('永久删除需要二次确认'))
        loaded = {os.path.abspath(engine.store.db_path): engine
                  for engine in self._engines.values()}
        try:
            candidates = self._delete_candidates(character, loaded)
        except Exception as exc:
            self.logger.warning('永久删除定位失败: %s', exc)
            return Err(SdkError('永久删除失败，请稍后重试'))
        failures: list[str] = []
        for path in candidates:
            engine = loaded.get(path)
            opened = None
            try:
                if engine is None:
                    opened = await asyncio.to_thread(
                        MemoryEngine, path, dict(self._settings))
                    engine = opened
                result = await asyncio.to_thread(
                    engine.delete_permanent, table, id)
            except Exception as exc:
                # 单个库打不开不影响继续找其他库；失败只记账，不中止
                failures.append(os.path.basename(path))
                self.logger.warning('永久删除失败(%s): %s', os.path.basename(path), exc)
                continue
            finally:
                if opened is not None:
                    await asyncio.to_thread(opened.store.close)
            if result.get('ok'):
                payload = dict(result)
                if failures:
                    payload['failed_databases'] = failures
                return Ok(payload)
        if failures:
            self.logger.warning('永久删除未完成，失败库: %s', ', '.join(failures))
            return Err(SdkError(
                f'有 {len(failures)} 个记忆库打不开，已跳过；其余库没有这条记忆'))
        return Err(SdkError('没有找到这条记忆'))

    @ui.action(label='永久删除', tone='danger', refresh_context=True)
    @plugin_entry(
        id='delete_working_turn',
        name='永久删除一条工作记忆',
        description='立即永久删除工作记忆里的一条原话，不受窗口滚动限制；不可撤销，需面板二次确认。',
        input_schema={
            'type': 'object',
            'properties': {
                'id': {'type': 'integer', 'description': '工作记忆条目 id'},
                'character': {
                    'type': 'string',
                    'description': '条目所属角色库，空串表示默认库（不传 null 才遍历所有库）',
                },
                'confirmed': {'type': 'boolean'},
            },
            'required': ['id', 'confirmed'],
        },
        metadata={'agent_hidden': True},
    )
    async def delete_working_turn(self, id: int = 0, character: str = '',
                                  confirmed: bool = False, **_):
        try:
            turn_id = int(id or 0)
        except (TypeError, ValueError):
            turn_id = 0
        if turn_id <= 0:
            return Err(SdkError('id 必填：请传入面板上的工作记忆条目 id'))
        if not confirmed:
            return Err(SdkError('永久删除需要二次确认'))
        loaded = {os.path.abspath(engine.store.db_path): engine
                  for engine in self._engines.values()}
        failures: list[str] = []
        # 空串必须是「默认库」：wm.id 是每库自增，跨库遍历会删到别的库同 id 的行
        for path in self._delete_candidates(character, loaded):
            engine = loaded.get(path)
            opened = None
            try:
                if engine is None:
                    opened = await asyncio.to_thread(
                        MemoryEngine, path, dict(self._settings))
                    engine = opened
                result = await asyncio.to_thread(
                    engine.delete_working_turn, turn_id)
            except Exception as exc:
                failures.append(os.path.basename(path))
                self.logger.warning('删除工作记忆失败(%s): %s',
                                    os.path.basename(path), exc)
                continue
            finally:
                if opened is not None:
                    await asyncio.to_thread(opened.store.close)
            if result.get('ok'):
                return Ok(result)
        if failures:
            return Err(SdkError(
                f'有 {len(failures)} 个记忆库打不开，已跳过；其余库没有这条工作记忆'))
        return Err(SdkError('没有找到这条工作记忆'))

    @ui.action(label='清空全部记忆', tone='danger', refresh_context=True)
    @plugin_entry(
        id='clear_memories',
        name='清空全部记忆',
        description='永久清空所有角色库的记忆、工作记忆、关联和撤销快照；不可撤销。',
        input_schema={
            'type': 'object',
            'properties': {'confirmed': {'type': 'boolean'}},
            'required': ['confirmed'],
        },
        metadata={'agent_hidden': True},
    )
    async def clear_memories(self, confirmed: bool = False, **_):
        if not confirmed:
            return Err(SdkError('清空全部记忆需要二次确认'))
        total: dict[str, Any] = {'ok': True, 'anchors': 0, 'semantics': 0,
                                 'working_turns': 0, 'revisions': 0,
                                 'databases': 0, 'failed': 0, 'reclaimed': True}
        loaded = {os.path.abspath(engine.store.db_path): engine
                  for engine in self._engines.values()}
        for path in self._db_file_paths():
            engine = loaded.get(path)
            opened = None
            try:
                if engine is None:
                    opened = await asyncio.to_thread(
                        MemoryEngine, path, dict(self._settings))
                    engine = opened
                one = await asyncio.to_thread(engine.clear_all)
            except Exception as exc:
                total['failed'] += 1
                self.logger.warning('清空记忆失败: %s', exc)
                continue
            finally:
                if opened is not None:
                    await asyncio.to_thread(opened.store.close)
            total['databases'] += 1
            if not one.get('reclaimed'):
                total['reclaimed'] = False
            for key in ('anchors', 'semantics', 'working_turns', 'revisions'):
                total[key] += int(one.get(key) or 0)
        total['ok'] = total['failed'] == 0
        total['backup_files'] = 0
        if total['ok']:
            total['backup_files'] = self._remove_migration_backups()
        return Ok(total)

    def _remove_migration_backups(self) -> int:
        """清空全部成功后，连带删掉迁移留下的 *.migrated：里面是旧库全文。"""
        parent = os.path.dirname(self._db_path())
        try:
            names = os.listdir(parent)
        except OSError:
            names = []
        removed = 0
        for name in names:
            if '.db.migrated' not in name:
                continue
            try:
                os.remove(os.path.join(parent, name))
            except OSError as exc:
                self.logger.warning('删除迁移备份失败(%s): %s', name, exc)
                continue
            removed += 1
        return removed

    @ui.action(label='清理旧记忆', refresh_context=True)
    @plugin_entry(
        id='purge_memories',
        name='清理旧记忆',
        description=(
            '永久删除所有已加载角色库中已移除超过 30 天或距最后一次确认超过 365 天的记忆，释放容量名额。'
            '只删未固定的条目，不可撤销；执行前建议先导出 JSON。'
        ),
        input_schema={'type': 'object', 'properties': {}},
        metadata={'agent_hidden': True},
    )
    async def purge_memories(self, **_):
        total: dict[str, Any] = {'ok': True, 'anchors': 0, 'semantics': 0,
                                 'total': 0, 'databases': 0, 'failed': 0}
        for engine in list(self._engines.values()):
            try:
                one = await asyncio.to_thread(engine.purge_stale)
            except Exception as exc:
                # 单个库清理失败不整体报错：其余库照常清理，失败数回给面板
                total['failed'] += 1
                self.logger.warning('清理旧记忆失败: %s', exc)
                continue
            total['databases'] += 1
            for key in ('anchors', 'semantics', 'total'):
                total[key] += int(one.get(key) or 0)
        total['ok'] = total['failed'] == 0
        return Ok(total)

    async def _memory_action(self, action: str, table: str, memory_id: str, **kwargs):
        if table not in {'anchors', 'semantics'} or not memory_id:
            return Err(SdkError('table 与 id 都必填'))
        if action == 'correct' and not kwargs:
            return Err(SdkError('没有需要纠正的字段'))
        results = []
        capacity_hit = False
        for engine in self._engines.values():
            try:
                if action == 'confirm':
                    result = await asyncio.to_thread(engine.confirm, table, memory_id)
                elif action == 'pin':
                    result = await asyncio.to_thread(
                        engine.pin, table, memory_id, bool(kwargs.get('pinned')))
                elif action == 'correct':
                    result = await asyncio.to_thread(
                        engine.correct, table, memory_id, **kwargs)
                else:
                    result = await asyncio.to_thread(engine.archive, table, memory_id)
            except Exception as exc:
                self.logger.warning('面板记忆操作 %s 失败: %s', action, exc)
                continue
            if result.get('ok'):
                results.append(result)
            elif result.get('reason') == 'capacity':
                capacity_hit = True
        if results:
            return Ok(results[0])
        if capacity_hit:
            return Err(SdkError('活跃/待确认记忆已达上限，请先移除或清理旧记忆'))
        return Err(SdkError('没有找到这条记忆'))

    # ------------------------------------------------------------------ 定时维护

    @timer_interval(id='maintenance', seconds=3600, name='休眠与分层维护', auto_start=True)
    async def on_maintenance_timer(self, **_):
        """每小时把久未确认的事件/偏好降为休眠，再重算热温冷。"""
        if not self._settings.get('enabled', True):
            # 总开关关闭 = 写入与召回全停，休眠沉降也一起暂停，避免无人使用时空转
            return Ok({'skipped': 'disabled', 'slept': {'anchors': 0, 'semantics': 0},
                       'moved': {'hot': 0, 'warm': 0, 'cold': 0}})
        slept = {'anchors': 0, 'semantics': 0}
        moved = {'hot': 0, 'warm': 0, 'cold': 0}
        for engine in list(self._engines.values()):
            result = await asyncio.to_thread(engine.maintenance)
            for key, value in (result.get('slept') or {}).items():
                slept[key] = slept.get(key, 0) + int(value or 0)
            for key, value in (result.get('retier') or {}).items():
                moved[key] = moved.get(key, 0) + int(value or 0)
        return Ok({'slept': slept, 'moved': moved})

    # ------------------------------------------------------------------ 内部

    def _character(self, kwargs: dict | None = None) -> str:
        """取角色名：优先宿主作用域 `ctx.current_lanlan`，再认宿主注入的 `_ctx`。"""
        ctx = getattr(self, 'ctx', None)
        name = getattr(ctx, 'current_lanlan', None) if ctx is not None else None
        if not name and isinstance(ctx, dict):
            name = ctx.get('lanlan_name')
        if name:
            return str(name)
        info = (kwargs or {}).get('_ctx')
        if isinstance(info, dict) and 'host_reply' in info and info.get('lanlan_name'):
            return str(info['lanlan_name'])
        return ''

    async def _load_settings(self) -> None:
        merged = dict(DEFAULT_SETTINGS)
        try:
            cfg = await self.config.dump(timeout=5.0)
            raw = (cfg or {}).get('settings') or {}
        except Exception as exc:
            self.logger.warning(f'读取配置失败，使用默认值: {exc}')
            self._settings = merged
            return
        if not isinstance(raw, dict):
            self.logger.warning('配置 settings 不是对象，使用默认值')
            self._settings = merged
            return
        for key, default in DEFAULT_SETTINGS.items():
            if key not in raw:
                continue
            try:
                if isinstance(default, bool):
                    value = (str(raw[key]).strip().lower() in {'1', 'true', 'yes', 'on'}
                             if isinstance(raw[key], str) else bool(raw[key]))
                else:
                    value = type(default)(raw[key])
            except (TypeError, ValueError, OverflowError):
                self.logger.warning(f'配置 {key} 类型不合法，保留默认值')
                continue
            if key in SETTING_RANGES:
                low, high = SETTING_RANGES[key]
                if not low <= value <= high:
                    self.logger.warning(f'配置 {key} 超出范围 {low}~{high}，保留默认值')
                    continue
            merged[key] = value
        self._settings = merged

    def _db_path(self, character: str = '') -> str:
        name = 'memory.db'
        if character:
            safe = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff_-]', '_', character)[:24] or 'default'
            digest = hashlib.sha256(character.encode('utf-8')).hexdigest()[:8]
            name = f'memory-{safe}-{digest}.db'
        data_dir = getattr(self, 'data_path', None)
        if callable(data_dir):
            return str(data_dir(name))
        storage = getattr(self, 'storage_dir', None) or PLUGIN_DIR
        return os.path.join(str(storage), 'data', name)

    def _legacy_db_path(self, character: str) -> str:
        """0.4.9 及以前的角色库名，没有哈希后缀。"""
        safe = re.sub(r'[^0-9A-Za-z\u4e00-\u9fff_-]', '_', character)[:24] or 'default'
        data_dir = getattr(self, 'data_path', None)
        if callable(data_dir):
            return str(data_dir(f'memory-{safe}.db'))
        storage = getattr(self, 'storage_dir', None) or PLUGIN_DIR
        return os.path.join(str(storage), 'data', f'memory-{safe}.db')

    @staticmethod
    def _db_owner(path: str) -> str:
        try:
            conn = sqlite3.connect(path, timeout=5)
            try:
                row = conn.execute(
                    "SELECT v FROM meta WHERE k='character'").fetchone()
                return str(row[0]) if row else ''
            finally:
                conn.close()
        except (sqlite3.Error, OSError):
            return ''

    @staticmethod
    def _move_db_files(source: str, target: str) -> None:
        try:
            os.replace(source, target)
        except OSError:
            shutil.copy2(source, target)
            try:
                os.replace(source, source + '.migrated')
            except OSError:
                pass
        for suffix in ('-wal', '-shm'):
            side = source + suffix
            if os.path.exists(side):
                try:
                    os.replace(side, target + suffix)
                except OSError:
                    pass

    def _character_db_paths(self) -> list[str]:
        parent = os.path.dirname(self._db_path())
        try:
            names = os.listdir(parent)
        except OSError:
            return []
        return [os.path.join(parent, name) for name in names
                if name.startswith('memory-') and name.endswith('.db')]

    def _db_file_paths(self) -> list[str]:
        """所有可能存在的插件库：已加载的、未加载的角色库和老 memory.db。"""
        paths = {os.path.abspath(engine.store.db_path)
                 for engine in self._engines.values()}
        parent = os.path.dirname(self._db_path())
        legacy = os.path.abspath(self._db_path())
        if os.path.exists(legacy):
            paths.add(legacy)
        try:
            names = os.listdir(parent)
        except OSError:
            names = []
        for name in names:
            if name.startswith('memory-') and name.endswith('.db'):
                paths.add(os.path.abspath(os.path.join(parent, name)))
        return sorted(paths)

    def _delete_candidates(self, character: str | None,
                           loaded: dict[str, Any]) -> list[str]:
        """单个角色只查它的库；没指定角色时按「已加载优先」把所有库排一遍。"""
        if character is not None:
            target = os.path.abspath(self._db_path(character))
            return [target] if target in loaded or os.path.exists(target) else []
        candidates = self._db_file_paths()
        candidates.sort(key=lambda path: (path not in loaded, path))
        return candidates

    def _migrate_legacy_role_db(self, character: str, target: str) -> None:
        legacy = self._legacy_db_path(character)
        if os.path.exists(target) or not os.path.exists(legacy):
            return
        owner = self._db_owner(legacy)
        if owner and owner != character:
            return
        self._move_db_files(legacy, target)

    def _adopt_legacy_db(self, target: str) -> None:
        legacy = self._db_path()
        if not os.path.exists(legacy):
            return
        engine = self._engines.pop('', None)
        if engine is not None:
            # 先把老库连接关掉并 checkpoint，否则 Windows 上 os.replace 会失败，
            # 复制主文件又会丢掉还在 WAL 里的最近写入。
            try:
                engine.store.close()
            except Exception as exc:
                self.logger.warning('关闭待迁移老库失败: %s', exc)
        try:
            os.replace(legacy, target)
        except OSError:
            shutil.copy2(legacy, target)
            try:
                os.replace(legacy, legacy + '.migrated')
            except OSError:
                pass
        for suffix in ('-wal', '-shm'):
            source = legacy + suffix
            if not os.path.exists(source):
                continue
            try:
                os.replace(source, target + suffix)
            except OSError:
                pass

    def _aggregate_stats(self) -> dict[str, Any]:
        total: dict[str, Any] = {'anchors': 0, 'semantics': 0, 'edges': 0,
                                 'working_turns': 0, 'turn_no': 0,
                                 'characters': len(self._engines),
                                 'fts_mode': 'unknown', 'pending_conflicts': 0,
                                 'unique_index_skipped': []}
        tiers = {'hot': 0, 'warm': 0, 'cold': 0}
        statuses: dict[str, int] = {}
        max_role_used = 0
        skipped: list[str] = []
        for engine in self._engines.values():
            stats = engine.store.stats()
            for key in ('anchors', 'semantics', 'edges', 'working_turns', 'turn_no',
                        'pending_conflicts'):
                total[key] += int(stats.get(key) or 0)
            total['fts_mode'] = stats.get('fts_mode') or total['fts_mode']
            for name in stats.get('unique_index_skipped') or []:
                if name not in skipped:
                    skipped.append(name)
            for key, value in (stats.get('tiers') or {}).items():
                tiers[key] = tiers.get(key, 0) + int(value or 0)
            engine_statuses = stats.get('statuses') or {}
            max_role_used = max(
                max_role_used,
                int(engine_statuses.get('active') or 0)
                + int(engine_statuses.get('pending') or 0),
            )
            for key, value in engine_statuses.items():
                statuses[key] = statuses.get(key, 0) + int(value or 0)
        total['tiers'] = tiers
        total['statuses'] = statuses
        total['max_role_used'] = max_role_used
        total['unique_index_skipped'] = skipped
        return total

    def _engine_error(self, character: str) -> str:
        if self._engine_errors.get(character) == 'capacity':
            return f'角色记忆库数量已达上限 {MAX_ENGINES}，请先导出并清理旧角色库'
        return '记忆引擎初始化失败，请检查插件数据目录是否可写'

    def _allow_write(self, character: str) -> bool:
        """每角色每分钟最多 30 次写入，防止模型刷条目把库撑爆。"""
        now = time.monotonic()
        recent = [stamp for stamp in self._write_times.get(character, [])
                  if now - stamp < WRITE_WINDOW_SECONDS]
        if len(recent) >= WRITE_LIMIT:
            self._write_times[character] = recent
            return False
        recent.append(now)
        self._write_times[character] = recent
        return True

    async def _ensure_engine(self, character: str = '') -> MemoryEngine | None:
        engine = self._engines.get(character)
        if engine is None and not character and len(self._engines) == 1:
            # 与老库迁移共用锁：迁移过程中不要把半个状态交给无角色调用
            async with self._init_lock:
                if len(self._engines) == 1:
                    return next(iter(self._engines.values()))
                engine = self._engines.get(character)
        if engine is not None:
            return engine
        async with self._init_lock:
            engine = self._engines.get(character)
            if engine is not None:
                return engine
            if len(self._engines) >= MAX_ENGINES:
                self.logger.warning('角色记忆库数量已达上限 %s，拒绝继续创建', MAX_ENGINES)
                self._engine_errors[character] = 'capacity'
                return None
            try:
                db_path = self._db_path(character)
                if character and not os.path.exists(db_path):
                    self._migrate_legacy_role_db(character, db_path)
                if character and not os.path.exists(db_path):
                    self._adopt_legacy_db(db_path)
                os.makedirs(os.path.dirname(db_path), exist_ok=True)
                engine = await asyncio.to_thread(MemoryEngine, db_path, dict(self._settings))
                if character and not engine.store.get_owner():
                    engine.store.set_owner(character)
                self._engines[character] = engine
                self._engine_errors.pop(character, None)
                self._ready = True
            except Exception as exc:
                self.logger.error(f'初始化记忆库失败: {exc}')
                self._ready = False
                return None
        return engine
