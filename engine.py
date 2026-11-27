"""调度器：记录原话证据，维护可否召回的生命周期。

v0.4 不再自动往对话里推记忆。写入只落本插件库；只有模型显式调用
`recall_temporal_memory` 或设置页读取时，才会看到仍然有效、证据完整的条目。
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Iterable
from typing import Any

from . import retrieval, scoring
from .memory_store import MemoryCapacityError, MemoryStore

MAX_EXPORT_ROWS_PER_TABLE = 10000

DEFAULT_SETTINGS: dict[str, Any] = {
    'enabled': True,
    'working_window': 8,
    'anchor_threshold': 0.3,
    'max_anchor_summary_chars': 140,
    'hot_recall_window_days': 7,
    'cold_after_days': 60,
    'dormancy_event_days': 90,
    'dormancy_preference_days': 180,
    'max_recall_anchors': 6,
    'max_recall_semantics': 12,
    'recall_char_budget': 1200,
    'recall_token_budget': 1200,
    'hypothesis_retention_days': 14,
    'export_sensitive_by_default': False,
    'consolidate_duplicates': True,
}

CONFIRM_SIMILARITY = 0.92

SENSITIVE_TAGS = frozenset({
    '里程碑', '纪念', '纪念日', '第一次', '生日', '周年', '情感', '告白', '求婚',
    'milestone', 'anniversary', 'birthday', 'emotional',
})


def _is_sensitive(item: dict[str, Any], table: str) -> bool:
    """里程碑/情感类默认不导出；标签或高情绪锚点都算。

    这是保守规则，不判断内容语义；用户可在导出面板显式勾选包含。
    """
    tags = str(item.get('tags') or '').lower().replace('，', ',')
    tag_set = {tag.strip() for tag in tags.split(',') if tag.strip()}
    if tag_set & SENSITIVE_TAGS:
        return True
    if table == 'anchors' and float(item.get('emotion') or 0.0) >= 0.75:
        return True
    return False


def _event_key(source_event_id: str, source_kind: str) -> str:
    """事件 id 按来源分命名空间，manual 不能抢占 chat_turn 的占位。"""
    if not source_event_id:
        return ''
    prefix = f'{source_kind}:'
    return (source_event_id if source_event_id.startswith(prefix)
            else prefix + source_event_id)


class MemoryEngine:
    def __init__(self, db_path: str, settings: dict[str, Any] | None = None) -> None:
        self.settings = {**DEFAULT_SETTINGS, **(settings or {})}
        self.store = MemoryStore(db_path)

    # ------------------------------------------------------------- 写入

    def observe(self, user_text: str, assistant_text: str = '',
                ts: int | None = None, *, source_event_id: str = '') -> dict[str, Any]:
        """一轮对话：原话进工作记忆，证据进插件库；不产生任何外发内容。"""
        ts = int(time.time()) if ts is None else int(ts)
        if not self.settings.get('enabled', True):
            return {'skipped': 'disabled', 'turn_no': 0, 'anchored': False,
                    'anchor': None, 'semantics': []}
        event_key = _event_key(source_event_id, 'chat_turn')
        try:
            with self.store.transaction():
                if event_key:
                    if not self.store.claim_event(event_key, 'chat_turn'):
                        return self._duplicate_result(event_key, 'chat_turn', manual=False)
                    # 占位可能因保留上限被清掉；事件级唯一索引仍在，重投时按已有记忆判重，
                    # 不再重复写工作记忆或重复 reconfirm。
                    if (self.store.find_anchor_by_event(event_key, 'chat_turn')
                            or self.store.semantics_for_event(event_key, 'chat_turn')):
                        return self._duplicate_result(event_key, 'chat_turn', manual=False)
                return self._observe_turn(user_text, assistant_text, ts=ts,
                                          source_event_id=event_key)
        except MemoryCapacityError:
            return {'skipped': 'capacity', 'turn_no': 0, 'anchored': False,
                    'anchor': None, 'semantics': []}

    def _duplicate_result(self, source_event_id: str, source_kind: str, *,
                          manual: bool) -> dict[str, Any]:
        anchor = self.store.find_anchor_by_event(source_event_id, source_kind)
        semantic_ids = [item.get('id') for item in
                        self.store.semantics_for_event(source_event_id, source_kind)]
        base = {'skipped': 'duplicate_event', 'duplicate': True,
                'anchored': bool(anchor), 'anchor_id': (anchor or {}).get('id'),
                'semantic_ids': semantic_ids}
        if manual:
            base['kind'] = (anchor or {}).get('kind') or ''
        else:
            base['turn_no'] = 0
        return base

    def _observe_turn(self, user_text: str, assistant_text: str, *, ts: int,
                      source_event_id: str) -> dict[str, Any]:
        turn_no = self.store.next_turn_no()
        keep = int(self.settings['working_window'])
        if user_text:
            self.store.add_turn('用户', user_text, ts=ts, keep=keep)
        if assistant_text:
            self.store.add_turn('助手', assistant_text, ts=ts, keep=keep)

        result = scoring.score_turn(
            user_text, assistant_text,
            threshold=float(self.settings['anchor_threshold']))
        payload: dict[str, Any] = {'score': result.as_dict(), 'anchored': False,
                                   'hypothesis': False,
                                   'anchor': None, 'semantics': [], 'turn_no': turn_no}
        if scoring.is_hypothesis(user_text):
            # 玩笑、假设、角色扮演设定单独进假设区：保留原话供核对，
            # 但不建事实/偏好、不参与召回，也不挤占锚点额度。
            anchor = self._create_hypothesis(user_text, ts=ts,
                                             time_label=scoring.time_label(ts, result.tags),
                                             tags=result.tags,
                                             source_event_id=source_event_id)
            payload.update({'hypothesis': True, 'anchor': anchor})
            return payload
        anchor: dict[str, Any] = {}
        if result.should_anchor and result.score >= float(self.settings['anchor_threshold']):
            anchor = self._create_anchor(
                user_text, assistant_text, result, ts=ts,
                time_label=scoring.time_label(ts, result.tags),
                source_event_id=source_event_id)
            payload.update({'anchored': True, 'anchor': anchor})

        # 偏好/事实即使没达到锚点阈值，也要留下可核对的证据；否则「我喜欢猫」永远进不来。
        semantics = self._ingest_semantics(
            user_text, tags=result.tags, ts=ts, source_event_id=source_event_id,
            attach_anchor_id=anchor.get('id') if anchor else None,
            verify_against=user_text)
        payload['semantics'] = semantics
        return payload

    def remember(self, text: str, ts: int | None = None,
                 tags: Iterable[str] | None = None, *, pinned: bool = False,
                 source_event_id: str = '') -> dict[str, Any]:
        """显式写入（用户说「记住」或 UI 手动加锚点），证据就是这句原话。"""
        if not self.settings.get('enabled', True):
            return {'skipped': 'disabled', 'anchor': None, 'kind': '', 'semantics': []}
        event_key = _event_key(source_event_id, 'manual')
        try:
            with self.store.transaction():
                if event_key:
                    if not self.store.claim_event(event_key, 'manual'):
                        return self._duplicate_result(event_key, 'manual', manual=True)
                    if (self.store.find_anchor_by_event(event_key, 'manual')
                            or self.store.semantics_for_event(event_key, 'manual')):
                        return self._duplicate_result(event_key, 'manual', manual=True)
                return self._remember_text(text, ts=ts, tags=tags, pinned=pinned,
                                           source_event_id=event_key)
        except MemoryCapacityError:
            return {'skipped': 'capacity', 'anchor': None, 'kind': '',
                    'semantics': []}

    def _remember_text(self, text: str, *, ts: int | None,
                       tags: Iterable[str] | None, pinned: bool,
                       source_event_id: str) -> dict[str, Any]:
        ts = int(time.time()) if ts is None else int(ts)
        tag_list = sorted({*(tags or ()), *scoring.extract_tags(text)})
        kind = scoring.classify_memory(text)
        verified = self._verify_against_working_memory(text)
        # 工作记忆里有用户原话但对不上 → 多半是模型转述而非原话，降级待确认；
        # 工作记忆为空时无从比对（null），维持原行为，不制造无谓的待确认。
        status = 'pending' if verified is False else 'active'
        anchor = self.store.add_anchor(
            summary=scoring.summarize(text, max_chars=int(self.settings['max_anchor_summary_chars'])),
            ts=ts,
            time_label=scoring.time_label(ts, tag_list),
            emotion=0.5,
            salience='hot',
            tags=tag_list,
            pinned=pinned,
            quote=text,
            source_event_id=source_event_id,
            source_kind='manual',
            kind=kind,
            score=1.0,
            status=status,
        )
        result = {'anchor': anchor, 'kind': kind, 'quote_verified': verified,
                  'semantics': self._ingest_semantics(
                      text, tags=tag_list, ts=ts, source_event_id=source_event_id,
                      attach_anchor_id=anchor['id'], source_kind='manual',
                      verify_against=text)}
        if verified is False:
            result['status'] = 'pending'
            result['reason'] = 'quote_not_in_working_memory'
            result['note'] = ('最近的工作记忆里没有这句用户原话，已放进「未分组待确认」：'
                              '核对后点「保留这条」才会进入召回。')
        return result

    def _create_hypothesis(self, user_text: str, *, ts: int, time_label: str,
                           tags: list[str], source_event_id: str = '') -> dict[str, Any]:
        """假设区条目：保留原话，但状态不是 active，召回和生命周期都不会碰它。"""
        return self.store.add_anchor(
            summary=scoring.summarize(
                user_text, max_chars=int(self.settings['max_anchor_summary_chars'])),
            ts=ts,
            time_label=time_label,
            emotion=0.2,
            salience='cold',
            tags=tags,
            pinned=False,
            quote=user_text,
            source_event_id=source_event_id,
            source_kind='chat_turn',
            kind='hypothesis',
            score=0.1,
            status='hypothesis',
        )

    def _create_anchor(self, user_text: str, assistant_text: str,
                       score: scoring.ScoreResult, *, ts: int,
                       time_label: str, source_event_id: str = '') -> dict[str, Any]:
        kind = scoring.classify_memory(user_text)
        pinned = False
        return self.store.add_anchor(
            summary=scoring.summarize(
                user_text, assistant_text,
                max_chars=int(self.settings['max_anchor_summary_chars'])),
            ts=ts,
            time_label=time_label,
            emotion=score.emotion,
            salience='hot' if 'explicit' in score.signals or score.emotion >= 0.8
            else scoring.salience_for(score.emotion, 'explicit' in score.signals),
            tags=score.tags,
            pinned=pinned,
            quote=user_text,
            source_event_id=source_event_id,
            source_kind='chat_turn',
            kind=kind,
            score=score.score,
        )

    def _verify_against_working_memory(self, text: str) -> bool | None:
        """拿最近工作记忆里的用户发言核对这句话是不是用户原话。

        返回 None 表示没有对照样本（工作记忆还是空的），调用方应维持原行为。
        """
        keep = max(8, int(self.settings.get('working_window') or 8))
        lines = [turn['content'] for turn in self.store.recent_turns(keep)
                 if turn.get('role') in ('用户', 'user') and turn.get('content')]
        if not lines:
            return None
        fragments = scoring.extract_memory_fragments(text, limit=4, min_chars=4)
        candidates = [fragment['content'] for fragment in fragments] or [text]
        return any(scoring.quote_matches(candidate, line)
                   for candidate in candidates for line in lines)

    def _ingest_semantics(self, text: str, *, tags: list[str], ts: int,
                          source_event_id: str, attach_anchor_id: str | None,
                          source_kind: str = 'chat_turn',
                          verify_against: str = '') -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        for fragment in scoring.extract_memory_fragments(text):
            verified = (True if not verify_against
                        else scoring.quote_matches(fragment['quote'], verify_against))
            item = self._add_or_confirm_semantic(
                fragment['content'], quote=fragment['quote'], kind=fragment['kind'],
                tags=tags, ts=ts, source_event_id=source_event_id,
                attach_anchor_id=attach_anchor_id, source_kind=source_kind,
                verified=verified)
            if item:
                out.append(item)
        return out

    def _add_or_confirm_semantic(self, content: str, *, quote: str, kind: str,
                                  tags: list[str], ts: int, source_event_id: str,
                                  attach_anchor_id: str | None,
                                  source_kind: str, verified: bool = True) -> dict[str, Any]:
        if not verified:
            # 摘出来的证据在原话里找不到：不参与确认/冲突判定，直接落未分组待确认，
            # 由用户核对后保留或归档，避免污染同一事实的确认计数。
            item = self.store.add_semantic(
                content, tags=tags, attach_anchor_id=attach_anchor_id, quote=quote,
                source_event_id=source_event_id, source_kind=source_kind,
                kind=kind, status='pending', score=0.5)
            self.store.link_event_semantic(source_event_id, source_kind, item['id'])
            return {**item, 'quote_verified': False, 'status': 'pending',
                    'reason': 'quote_not_in_source'}
        candidates = self.store.recent_semantics(kind=kind)
        for old in candidates:
            if old.get('status') in {'pending', 'archived'}:
                continue
            old_text = old.get('content') or ''
            sim = scoring.similarity(old_text, content)
            if sim >= CONFIRM_SIMILARITY:
                self.store.set_memory_state(
                    'semantics', old['id'], confirm=True, action='reconfirm',
                    record_revision=False)
                self.store.link_event_semantic(source_event_id, source_kind, old['id'])
                return {'id': old['id'], 'kind': old.get('kind') or kind,
                        'status': 'active', 'confirmed': True}
            if kind == 'preference' and old.get('kind') == 'preference':
                old_pred, old_obj = scoring.preference_parts(old_text)
                new_pred, new_obj = scoring.preference_parts(content)
                same_polarity = scoring.preference_negated(old_text) == \
                    scoring.preference_negated(content)
                if old_pred and new_pred and same_polarity and old_obj and new_obj \
                        and scoring.similarity(old_obj, new_obj) >= 0.6:
                    self.store.set_memory_state(
                        'semantics', old['id'], confirm=True, action='reconfirm',
                        record_revision=False)
                    self.store.link_event_semantic(source_event_id, source_kind, old['id'])
                    return {'id': old['id'], 'kind': old.get('kind') or kind,
                            'status': 'active', 'confirmed': True}
            if scoring.looks_conflicting(old, content, kind):
                group = f'cfx_{uuid.uuid4().hex[:10]}'
                new = self.store.materialize_conflict(
                    'semantics', old['id'],
                    {'content': content, 'tags': tags, 'attach_anchor_id': attach_anchor_id,
                     'source_kind': source_kind},
                    group, quote=quote, source_event_id=source_event_id,
                    kind=kind, score=0.5)
                if new.get('ok'):
                    self.store.link_event_semantic(
                        source_event_id, source_kind, new['new']['id'])
                    return new['new']
                return {}
        item = self.store.add_semantic(
            content, tags=tags, attach_anchor_id=attach_anchor_id, quote=quote,
            source_event_id=source_event_id, source_kind=source_kind,
            kind=kind, status='active', score=0.5)
        self.store.link_event_semantic(source_event_id, source_kind, item['id'])
        return item

    # ------------------------------------------------------------- 读取

    def recall(self, query: str, exclude_ids: Iterable[str] = (),
               *, count_as_recall: bool = True) -> dict[str, Any]:
        """只读召回：默认只返回 active 且带原话证据的条目。"""
        return retrieval.recall(
            self.store, query,
            working_window=int(self.settings['working_window']),
            max_anchors=int(self.settings['max_recall_anchors']),
            max_semantics=int(self.settings['max_recall_semantics']),
            budget_chars=int(self.settings['recall_char_budget']),
            budget_tokens=int(self.settings['recall_token_budget']),
            exclude_ids=tuple(exclude_ids),
            count_as_recall=count_as_recall,
        )

    def timeline(self, start_ts: int | None = None, end_ts: int | None = None,
                 limit: int = 30) -> dict[str, Any]:
        return retrieval.timeline(self.store, start_ts=start_ts, end_ts=end_ts, limit=limit)

    def list_memories(self, limit: int = 400,
                      statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        now = int(time.time())
        rows: list[dict[str, Any]] = []
        for anchor in self.store.all_memories('anchors', limit, statuses=statuses):
            rows.append(self._decorate(anchor, 'anchors', now, text_key='summary'))
        for semantic in self.store.all_memories('semantics', limit, statuses=statuses):
            rows.append(self._decorate(semantic, 'semantics', now, text_key='content'))
        rows.sort(key=lambda item: (item.get('pinned') not in (1, True),
                                    item.get('status') != 'active',
                                    -int(item.get('ts') or 0)))
        return rows[:limit]

    def _decorate(self, item: dict[str, Any], table: str, now: int, *,
                  text_key: str) -> dict[str, Any]:
        created = int(item.get('last_confirmed_at') or item.get('created_at') or 0)
        age_days = max(0.0, (now - created) / 86400)
        kind = item.get('kind') or ('event' if table == 'anchors' else 'fact')
        horizon = float(self.settings['dormancy_event_days'] if kind == 'event'
                        else self.settings['dormancy_preference_days'])
        base = float(item.get('score') or 0.0)
        if item.get('pinned'):
            effective = 1.0
        elif item.get('status') != 'active':
            effective = 0.0
        else:
            effective = round(base * (0.5 ** (age_days / max(1.0, horizon))), 3)
        return {
            **item,
            'ts': int(item.get('ts') or item.get('created_at') or 0),
            'table': table,
            'text': item.get(text_key) or '',
            'effective_score': effective,
            'age_days': round(age_days, 1),
            'next_sleep_days': None if item.get('pinned') else
            round(max(0.0, horizon - age_days), 1),
            'can_recall': item.get('status') == 'active' and bool(item.get('quote')),
            'sensitive': _is_sensitive(item, table),
        }

    # ------------------------------------------------------------- 面板维护

    def confirm(self, table: str, memory_id: str) -> dict[str, Any]:
        return self.store.confirm_memory(table, memory_id)

    def pin(self, table: str, memory_id: str, pinned: bool) -> dict[str, Any]:
        return self.store.set_pinned(table, memory_id, pinned)

    def correct(self, table: str, memory_id: str, **fields: Any) -> dict[str, Any]:
        return self.store.revise_memory(table, memory_id, action='correct', **fields)

    def archive(self, table: str, memory_id: str) -> dict[str, Any]:
        return self.store.archive_memory(table, memory_id)

    def resolve_orphan(self, table: str, memory_id: str,
                       keep: bool = True) -> dict[str, Any]:
        return self.store.resolve_orphan_pending(table, memory_id, keep)

    def delete_permanent(self, table: str, memory_id: str) -> dict[str, Any]:
        return self.store.delete_permanent(table, memory_id)

    def delete_working_turn(self, turn_id: int) -> dict[str, Any]:
        ok = self.store.delete_working_turn(turn_id)
        return ({'ok': True, 'id': int(turn_id)} if ok
                else {'ok': False, 'reason': 'not_found'})

    def clear_all(self) -> dict[str, Any]:
        return self.store.clear_all()

    def resolve_conflict(self, group: str, keep_id: str) -> dict[str, Any]:
        return self.store.resolve_conflict(group, keep_id)

    def undo(self) -> dict[str, Any]:
        return self.store.undo_last()

    def export(self, max_rows: int = MAX_EXPORT_ROWS_PER_TABLE, *,
               include_sensitive: bool | None = None) -> dict[str, Any]:
        cap = max(1, int(max_rows))
        if include_sensitive is None:
            include_sensitive = bool(self.settings.get('export_sensitive_by_default', False))
        anchors = self.store.all_memories('anchors', cap + 1)
        semantics = self.store.all_memories('semantics', cap + 1)
        redacted = 0
        if not include_sensitive:
            kept_anchors = [item for item in anchors if not _is_sensitive(item, 'anchors')]
            kept_semantics = [item for item in semantics if not _is_sensitive(item, 'semantics')]
            redacted = (len(anchors) - len(kept_anchors)) + (len(semantics) - len(kept_semantics))
            anchors, semantics = kept_anchors, kept_semantics
        anchors_truncated = len(anchors) > cap
        semantics_truncated = len(semantics) > cap
        window = self.settings.get('working_window')
        window = 8 if window is None else max(0, int(window))
        return {
            'exported_at': int(time.time()),
            'schema': 4,
            'anchors': anchors[:cap],
            'semantics': semantics[:cap],
            'working_memory': self.store.recent_turns(window),
            'truncated': {'anchors': anchors_truncated,
                          'semantics': semantics_truncated},
            'redacted_sensitive': redacted,
            'include_sensitive': include_sensitive,
        }

    def clear_sensitive(self) -> dict[str, Any]:
        """永久清除里程碑/情感类条目；只在本插件私有库内执行，不可撤销。"""
        total = {'ok': True, 'anchors': 0, 'semantics': 0, 'total': 0}
        for table in ('anchors', 'semantics'):
            for item in self.store.all_memories(table, MAX_EXPORT_ROWS_PER_TABLE):
                if not _is_sensitive(item, table):
                    continue
                result = self.store.delete_permanent(table, item['id'])
                if result.get('ok'):
                    total[table] += 1
                    total['total'] += 1
        return total

    def consolidate(self, now: int | None = None) -> dict[str, int]:
        """只整理本插件自己的库：过期假设、完全重复条目。

        - 假设区超过保留天数：永久删除（原话只用于核对情境，不是事实）。
        - 同表、同 kind、完全同文的 active 条目：保留分数最高/最近确认的一条，其余归档。
        不动正文不同的近似项，也不合并跨角色库，避免把用户的不同记忆揉成一条。
        """
        now = int(time.time()) if now is None else int(now)
        retention = max(1, int(self.settings.get('hypothesis_retention_days') or 14))
        cutoff = now - retention * 86400
        counts = {'hypotheses': 0, 'anchors': 0, 'semantics': 0}
        anchors = self.store.all_memories('anchors', MAX_EXPORT_ROWS_PER_TABLE)
        for item in anchors:
            if item.get('status') == 'hypothesis' and int(item.get('ts') or 0) < cutoff:
                if self.store.delete_permanent('anchors', item['id']).get('ok'):
                    counts['hypotheses'] += 1
        if not self.settings.get('consolidate_duplicates', True):
            return counts
        anchors = [item for item in self.store.all_memories(
            'anchors', MAX_EXPORT_ROWS_PER_TABLE) if item.get('status') == 'active']
        semantics = [item for item in self.store.all_memories(
            'semantics', MAX_EXPORT_ROWS_PER_TABLE) if item.get('status') == 'active']
        for table, items, text_key, count_key in (
                ('anchors', anchors, 'quote', 'anchors'),
                ('semantics', semantics, 'content', 'semantics')):
            groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
            for item in items:
                key = (str(item.get('kind') or ''), str(item.get(text_key) or '').strip())
                if key[1]:
                    groups.setdefault(key, []).append(item)
            for group in groups.values():
                if len(group) < 2:
                    continue
                group.sort(key=lambda item: (
                    -float(item.get('score') or 0.0),
                    -int(item.get('last_confirmed_at') or 0),
                    -int(item.get('created_at') or 0),
                ))
                for duplicate in group[1:]:
                    if self.store.archive_memory(table, duplicate['id']).get('ok'):
                        counts[count_key] += 1
        return counts

    def purge_stale(self, *, archived_days: int = 30,
                    dormant_days: int = 365) -> dict[str, Any]:
        return self.store.purge_stale(archived_days=archived_days,
                                      dormant_days=dormant_days)

    # ------------------------------------------------------------- 维护

    def apply_lifecycle(self, now: int | None = None) -> dict[str, int]:
        return self.store.lifecycle(
            now,
            event_days=int(self.settings['dormancy_event_days']),
            preference_days=int(self.settings['dormancy_preference_days']))

    def retier(self, now: int | None = None) -> dict[str, int]:
        """热温冷重算：最近常被召回 → hot；长期未召回且低情绪 → cold。"""
        now = int(time.time()) if now is None else int(now)
        return self.store.retier(
            now,
            hot_window=int(self.settings['hot_recall_window_days']) * 86400,
            cold_after=int(self.settings['cold_after_days']) * 86400)

    def maintenance(self, now: int | None = None) -> dict[str, Any]:
        slept = self.apply_lifecycle(now)
        consolidated = self.consolidate(now)
        return {'slept': slept, 'retier': self.retier(now),
                'consolidated': consolidated,
                'pruned_events': self.store.prune_processed_events(now)}

    def stats(self) -> dict[str, Any]:
        return self.store.stats()
