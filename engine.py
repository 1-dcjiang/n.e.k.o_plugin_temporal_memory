"""调度器：记录原话证据，维护可否召回的生命周期。

v0.4 不再自动往对话里推记忆。写入只落本插件库；只有模型显式调用
`recall_temporal_memory` 或设置页读取时，才会看到仍然有效、证据完整的条目。
"""

from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Iterable
from typing import Any

from . import retrieval, scoring
from .memory_store import MemoryCapacityError, MemoryStore

MAX_EXPORT_ROWS_PER_TABLE = 10000
MAX_HYPOTHESES = 200
_LOG = logging.getLogger(__name__)

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
    '纪念', '纪念日', '生日', '周年', '情感', '告白', '求婚',
    'anniversary', 'birthday', 'emotional',
})

# observe 的标签白名单只有领域词，自动标签永远不会有「生日」；敏感判定必须看正文。
SENSITIVE_TEXT_TERMS = (
    '生日', '诞生日', '纪念日', '结婚', '婚礼', '求婚',
    '初恋', '初吻', '分手', '第一次一起', '第一次约', '第一次告白',
    '第一次接吻',
)

# milestone/proposal/第一次见/confession 在开发与商务语境里很常见，不能裸词命中；
# 只有和情感语境同现时才按敏感处理。英文词加词边界，并排除
# birthday attack / Windows Anniversary Update 这类技术固定搭配。
SENSITIVE_TEXT_PATTERNS = (
    re.compile(
        r'(?:感情|恋爱|婚姻|爱情|人生).{0,12}里程碑|'
        r'里程碑.{0,12}(?:感情|恋爱|婚姻|爱情|人生)'),
    re.compile(
        r'(?:结婚|婚礼|求婚|marriage|wedding).{0,20}(?:proposal|milestone)|'
        r'(?:proposal|milestone).{0,20}(?:结婚|婚礼|求婚|marriage|wedding)'),
    re.compile(
        r'(?:第一次见|初见)(?:他|她|你|对方|这个人|那个人|面).{0,12}'
        r'(?:心动|喜欢|爱|害羞|难忘)'),
    re.compile(
        r'(?:confession).{0,20}(?:love|feelings|romantic)|'
        r'(?:love|feelings|romantic).{0,20}(?:confession)'),
    re.compile(
        r'(?:结婚|婚礼|求婚|恋爱|感情|相爱).{0,10}周年|'
        r'周年.{0,10}(?:结婚|婚礼|求婚|恋爱|感情|相爱)'),
    # 中文没有词边界：「广告白名单/公告白名单」会跨词拼出「告白」。
    re.compile(r'(?<![广公报])告白'),
    re.compile(
        r'(?<![a-z])birthday(?!\s+(?:attack|paradox|problem|bound|collision))'
        r'(?![a-z])'),
    re.compile(
        r'(?<![a-z])anniversary(?!\s+(?:update|edition|release))(?![a-z])'),
)


def is_sensitive_text(text: Any) -> bool:
    """按正文判断里程碑/情感内容：不依赖模型有没有主动打标签。"""
    normalized = str(text or '').lower().replace('，', ',')
    if any(term in normalized for term in SENSITIVE_TEXT_TERMS):
        return True
    return any(pattern.search(normalized) for pattern in SENSITIVE_TEXT_PATTERNS)


def _sensitive_reasons(item: dict[str, Any], table: str) -> tuple[str, ...]:
    """返回敏感原因，供预览区分正文/标签/仅情绪命中。"""
    reasons: list[str] = []
    tags = str(item.get('tags') or '').lower().replace('，', ',')
    tag_set = {tag.strip() for tag in tags.split(',') if tag.strip()}
    if tag_set & SENSITIVE_TAGS:
        reasons.append('tag')
    for key in ('quote', 'summary', 'content'):
        if is_sensitive_text(item.get(key)):
            reasons.append('text')
            break
    if table == 'anchors' and float(item.get('emotion') or 0.0) >= 0.75:
        reasons.append('emotion')
    return tuple(reasons)


def _is_sensitive(item: dict[str, Any], table: str) -> bool:
    """里程碑/情感类默认不导出；标签、正文或高情绪锚点都算。

    这是保守规则，不判断内容语义；用户可在导出面板显式勾选包含。
    """
    return bool(_sensitive_reasons(item, table))


def _redact_working_turns(turns: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """导出/清除时连同敏感用户发言的紧邻助手回复一起过滤，避免复述泄露日期。"""
    kept: list[dict[str, Any]] = []
    redacted = 0
    previous_user_sensitive = False
    for turn in turns:
        role = str(turn.get('role') or '').lower()
        is_user = role in {'用户', 'user'}
        is_assistant = role in {'助手', 'assistant'}
        hit = is_sensitive_text(turn.get('content'))
        if is_assistant and previous_user_sensitive:
            hit = True
        if hit:
            redacted += 1
            previous_user_sensitive = is_user
            continue
        kept.append(turn)
        previous_user_sensitive = False
    return kept, redacted


def _hypothesis_age_base(item: dict[str, Any]) -> int:
    """假设区寿命按真实写入时间算；ts 可被模型传入，不能当保留期起点。"""
    return int(item.get('created_at') or item.get('ts') or 0)


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
        self._residue_purge_pending = False

    # ------------------------------------------------------------- 写入

    def observe(self, user_text: str, assistant_text: str = '',
                ts: int | None = None, *, source_event_id: str = '') -> dict[str, Any]:
        """一轮对话：原话进工作记忆，证据进插件库；不产生任何外发内容。"""
        ts = int(time.time()) if ts is None else int(ts)
        if not self.settings.get('enabled', True):
            return {'skipped': 'disabled', 'turn_no': 0, 'anchored': False,
                    'anchor': None, 'semantics': []}
        event_key = _event_key(source_event_id, 'chat_turn')
        result: dict[str, Any] | None = None
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
                result = self._observe_turn(user_text, assistant_text, ts=ts,
                                            source_event_id=event_key)
        except MemoryCapacityError:
            return {'skipped': 'capacity', 'turn_no': 0, 'anchored': False,
                    'anchor': None, 'semantics': []}
        # 假设区淘汰的旧条目已删除，但 WAL/页残留收尾必须等事务提交后再做；
        # 否则 purge 里的写事务会等待当前 observe 持到的写锁，超时后放弃。
        if self._residue_purge_pending:
            self._residue_purge_pending = False
            try:
                self.store.purge_delete_residue()
            except Exception as exc:
                _LOG.warning('假设区淘汰后的残留清理失败: %s', exc)
        return result if result is not None else {
            'skipped': 'no_result', 'turn_no': 0, 'anchored': False,
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
        if (scoring.is_hypothesis(user_text)
                and result.raw_score >= float(self.settings['anchor_threshold'])):
            # 玩笑、假设、角色扮演设定单独进假设区：保留原话供核对，
            # 但不建事实/偏好、不参与召回，也不挤占锚点额度。
            anchor = self._create_hypothesis(user_text, ts=ts,
                                             time_label=scoring.time_label(ts, result.tags),
                                             tags=result.tags,
                                             source_event_id=source_event_id)
            payload.update({'hypothesis': True, 'anchor': anchor})
            return payload
        if scoring.is_hypothesis(user_text):
            # 假设句也没过阈值：不落库，避免靠假设分支绕开稀疏策略。
            payload['hypothesis'] = True
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
        rows = self.store.all_memories('anchors', limit=None, statuses=('hypothesis',))
        if len(rows) >= MAX_HYPOTHESES:
            # 假设区也有上限：先删最老的，避免玩笑句无限占用明文库。
            for item in sorted(rows, key=_hypothesis_age_base)[
                    :len(rows) - MAX_HYPOTHESES + 1]:
                if self.store.delete_permanent(
                        'anchors', item['id'], purge_residue=False).get('ok'):
                    self._residue_purge_pending = True
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
                user_text, max_chars=int(self.settings['max_anchor_summary_chars'])),
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
        retention = max(1, int(self.settings.get('hypothesis_retention_days') or 14))
        hypothesis_left = None
        if item.get('status') == 'hypothesis':
            hypothesis_left = max(0.0, (
                _hypothesis_age_base(item) + retention * 86400 - now) / 86400)
        return {
            **item,
            'ts': int(item.get('ts') or item.get('created_at') or 0),
            'table': table,
            'text': item.get(text_key) or '',
            'effective_score': effective,
            'age_days': round(age_days, 1),
            'next_sleep_days': None if item.get('pinned') or
            item.get('status') == 'hypothesis' else
            round(max(0.0, horizon - age_days), 1),
            'delete_in_days': None if hypothesis_left is None
            else round(hypothesis_left, 1),
            'can_recall': item.get('status') == 'active' and bool(item.get('quote')),
            'sensitive': _is_sensitive(item, table),
        }

    # ------------------------------------------------------------- 面板维护

    def confirm(self, table: str, memory_id: str) -> dict[str, Any]:
        kind = None
        if table == 'anchors':
            item = self.store.get_memory(table, memory_id)
            if item and item.get('kind') == 'hypothesis':
                # 用户确认「假设」其实是真的：改回普通事件类型，避免面板/导出
                # 一直把它当假设。kind 与 status 同一次更新，只留一条撤销记录。
                kind = scoring.classify_memory(item.get('quote') or '')
        return self.store.confirm_memory(table, memory_id, kind=kind)

    def pin(self, table: str, memory_id: str, pinned: bool) -> dict[str, Any]:
        if pinned and table == 'anchors':
            item = self.store.get_memory(table, memory_id)
            if item and item.get('status') == 'hypothesis':
                # 固定假设行不能只翻 status：先按 confirm 的逻辑重分类，
                # 否则会留下 pinned + 可召回 + kind=hypothesis 的误导条目。
                confirmed = self.confirm(table, memory_id)
                if not confirmed.get('ok'):
                    return confirmed
        return self.store.set_pinned(table, memory_id, pinned)

    def correct(self, table: str, memory_id: str, **fields: Any) -> dict[str, Any]:
        return self.store.revise_memory(table, memory_id, action='correct', **fields)

    def archive(self, table: str, memory_id: str) -> dict[str, Any]:
        return self.store.archive_memory(table, memory_id)

    def resolve_orphan(self, table: str, memory_id: str,
                       keep: bool = True) -> dict[str, Any]:
        return self.store.resolve_orphan_pending(table, memory_id, keep)

    def delete_permanent(self, table: str, memory_id: str, *,
                         purge_residue: bool = True) -> dict[str, Any]:
        return self.store.delete_permanent(
            table, memory_id, purge_residue=purge_residue)

    def delete_working_turn(self, turn_id: int, *,
                            purge_residue: bool = True) -> dict[str, Any]:
        ok = self.store.delete_working_turn(
            turn_id, purge_residue=purge_residue)
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
        # 先取全量再过滤：如果先取 cap+1 再过滤，前 1 万条全是敏感条目时
        # 第 10001 条普通条目会被静默藏掉，truncated 也会误报。
        anchors = self.store.all_memories('anchors', limit=None)
        semantics = self.store.all_memories('semantics', limit=None)
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
        working = self.store.recent_turns(window)
        if not include_sensitive:
            working, working_redacted = _redact_working_turns(working)
            redacted += working_redacted
        return {
            'exported_at': int(time.time()),
            'schema': 4,
            'anchors': anchors[:cap],
            'semantics': semantics[:cap],
            'working_memory': working,
            'truncated': {'anchors': anchors_truncated,
                          'semantics': semantics_truncated},
            'redacted_sensitive': redacted,
            'include_sensitive': include_sensitive,
        }

    def preview_sensitive(self) -> dict[str, Any]:
        """清除前 dry-run：返回命中数量与前几条预览，不删除任何数据。"""
        result: dict[str, Any] = {'ok': True, 'anchors': 0, 'semantics': 0,
                                  'working': 0, 'total': 0, 'backups': 0,
                                  'backups_unreadable': 0, 'samples': []}
        for table in ('anchors', 'semantics'):
            for item in self.store.all_memories(table, limit=None):
                if not _is_sensitive(item, table):
                    continue
                result[table] += 1
                result['total'] += 1
                if len(result['samples']) < 5:
                    text = (item.get('quote') or item.get('summary')
                            or item.get('content') or '')
                    result['samples'].append({
                        'table': table,
                        'id': str(item.get('id') or ''),
                        'text': str(text)[:80],
                        'reason': ','.join(_sensitive_reasons(item, table)),
                        'pinned': bool(item.get('pinned')),
                    })
        all_turns = self.store.all_working_turns()
        kept_turns, _ = _redact_working_turns(all_turns)
        result['working'] = len(all_turns) - len(kept_turns)
        result['total'] += result['working']
        return result

    def clear_sensitive(self) -> dict[str, Any]:
        """永久清除里程碑/情感类条目；只在本插件私有库内执行，不可撤销。"""
        total = {'ok': True, 'anchors': 0, 'semantics': 0,
                 'working': 0, 'total': 0}
        deleted = 0
        deleted_working = 0
        for table in ('anchors', 'semantics'):
            for item in self.store.all_memories(table, limit=None):
                if not _is_sensitive(item, table):
                    continue
                # 逐条删除只做页面清零；FTS optimize/WAL checkpoint 留到循环后统一收尾。
                result = self.store.delete_permanent(
                    table, item['id'], purge_residue=False)
                if result.get('ok'):
                    total[table] += 1
                    total['total'] += 1
                    deleted += 1
        all_turns = self.store.all_working_turns()
        kept_turns, _ = _redact_working_turns(all_turns)
        kept_ids = {int(turn['id']) for turn in kept_turns}
        for turn in all_turns:
            if int(turn['id']) in kept_ids:
                continue
            if self.store.delete_working_turn(
                    int(turn['id']), purge_residue=False):
                total['working'] += 1
                total['total'] += 1
                deleted_working += 1
        if deleted or deleted_working:
            # 一次性收尾：避免每删一行都开连接做 optimize/checkpoint。
            self.store.purge_delete_residue()
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
        anchors = self.store.all_memories('anchors', limit=None)
        for item in anchors:
            created = _hypothesis_age_base(item)
            if item.get('status') == 'hypothesis' and created < cutoff:
                if self.store.delete_permanent(
                        'anchors', item['id'], purge_residue=False).get('ok'):
                    counts['hypotheses'] += 1
        if counts['hypotheses']:
            self.store.purge_delete_residue()
        if not self.settings.get('consolidate_duplicates', True):
            return counts
        anchors = [item for item in self.store.all_memories(
            'anchors', limit=None) if item.get('status') == 'active']
        semantics = [item for item in self.store.all_memories(
            'semantics', limit=None) if item.get('status') == 'active']
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
                    -int(item.get('pinned') or 0),
                    -float(item.get('score') or 0.0),
                    -int(item.get('last_confirmed_at') or 0),
                    -int(item.get('created_at') or 0),
                ))
                for duplicate in group[1:]:
                    if duplicate.get('pinned'):
                        # 固定是用户明确要求长期保留，不参与自动去重。
                        continue
                    result = self.store.set_memory_state(
                        table, duplicate['id'], status='archived',
                        action='consolidate', record_revision=False,
                        require_unpinned_active=True)
                    if result.get('ok'):
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
