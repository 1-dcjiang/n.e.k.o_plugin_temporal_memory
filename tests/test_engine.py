"""调度器：写入阈值、热温冷迁移、证据召回与休眠生命周期。"""

from __future__ import annotations

import pytest
from temporal_memory.engine import MemoryEngine


@pytest.fixture()
def engine(tmp_path):
    return MemoryEngine(str(tmp_path / 'memory.db'))


def test_mundane_turn_writes_no_anchor(engine):
    result = engine.observe('今天天气不错', ts=1000)
    assert result['anchored'] is False
    assert result['turn_no'] == 1
    assert engine.store.stats()['anchors'] == 0
    assert engine.store.recent_turns(8)[-1]['content'] == '今天天气不错'


def test_explicit_turn_creates_hot_anchor(engine):
    result = engine.observe('记住，我的学号是 2614401064', '好的，记住了', ts=1000)
    assert result['anchored'] is True
    assert result['anchor']['salience'] == 'hot'
    assert engine.store.stats()['anchors'] == 1


def test_remember_is_unconditional(engine):
    out = engine.remember('2016 年第一次去北京', ts=1000)
    assert out['anchor']['salience'] == 'hot'
    assert engine.store.stats()['anchors'] == 1


def test_disabled_engine_rejects_explicit_remember(tmp_path):
    eng = MemoryEngine(str(tmp_path / 'memory.db'), {'enabled': False})
    out = eng.remember('我喜欢猫', ts=1000)
    assert out['skipped'] == 'disabled'
    assert eng.store.stats()['anchors'] == 0
    assert eng.store.stats()['semantics'] == 0


def test_recall_returns_prompt_block(engine):
    engine.remember('全飞秒术后要按医嘱滴眼药水，别揉眼睛', ts=1000)
    result = engine.recall('全飞秒 眼药水')
    assert result['prompt_block']
    assert result['chars'] == len(result['prompt_block'])
    assert result['chars'] <= engine.settings['recall_char_budget']


def test_recall_block_carries_quote_when_summary_differs(engine):
    engine.store.add_anchor(
        summary='猫毛过敏', quote='我对猫毛过敏，进猫舍要戴口罩', kind='fact', score=0.5)
    result = engine.recall('猫毛 过敏')
    assert '原话：' in result['prompt_block']
    assert '戴口罩' in result['prompt_block']


def test_timeline_only_returns_window(engine):
    engine.remember('一月的决定', ts=1_700_000_000)
    engine.remember('二月的决定', ts=1_700_200_000)
    out = engine.timeline(start_ts=1_700_100_000, end_ts=1_700_300_000)
    assert out['count'] == 1
    assert '二月' in out['text']


def test_retier_demotes_untouched_old_anchor(engine):
    engine.store.add_anchor(summary='很久以前的一件小事', ts=1_000_000,
                            emotion=0.0, salience='warm')
    moved = engine.retier(now=1_000_000 + 90 * 86400)
    assert moved['cold'] == 1
    assert engine.store.all_anchors()[0]['salience'] == 'cold'


def test_retier_promotes_frequently_recalled(engine):
    engine.store.add_anchor(summary='全飞秒术后复查', ts=1_000_000,
                            emotion=0.0, salience='warm')
    anchor_id = engine.store.all_anchors()[0]['id']
    # 同一轮重复命中只计一次；两次不同轮次才够 hot 门槛
    engine.store.bump_recall([anchor_id, anchor_id])
    assert engine.store.all_anchors()[0]['recall_count'] == 1
    engine.store.bump_recall([anchor_id])
    moved = engine.retier(now=1_000_000 + 2 * 86400)
    assert moved['hot'] == 1


def test_no_passive_injection_api(engine):
    # 原版注入入口必须不存在，避免以后又接回去
    assert not hasattr(engine, 'decide_injection')
    assert not hasattr(engine, 'commit_injection')
    result = engine.observe('记住，我对猫毛过敏', ts=1000)
    assert 'injection' not in result


def test_event_sleeps_after_90_days_but_pinned_survives(engine):
    anchor = engine.store.add_anchor(
        summary='旧事件', quote='旧事件', ts=1000, kind='event', score=0.5)
    pinned = engine.store.add_anchor(
        summary='固定事件', quote='固定事件', ts=1000, kind='event',
        score=0.5, pinned=True)
    now = int(anchor['created_at']) + 91 * 86400
    moved = engine.apply_lifecycle(now=now)
    assert moved['anchors'] == 1
    assert engine.store.get_anchor(anchor['id'])['status'] == 'dormant'
    assert engine.store.get_anchor(pinned['id'])['status'] == 'active'
    assert engine.store.get_anchor(pinned['id'])['pinned'] == 1


def test_preference_sleeps_after_180_days(engine):
    sem = engine.store.add_semantic(
        '我喜欢喝美式', quote='我喜欢喝美式', kind='preference')
    row = engine.store.get_memory('semantics', sem['id'])
    now = int(row['created_at'])
    moved = engine.apply_lifecycle(now=now + 181 * 86400)
    assert moved['semantics'] == 1
    assert engine.store.get_memory('semantics', sem['id'])['status'] == 'dormant'


def test_conflict_is_pending_and_not_recalled_until_resolved(engine):
    old = engine.store.add_semantic(
        '我不喜欢猫', quote='我不喜欢猫', kind='preference')
    new = engine._add_or_confirm_semantic(
        '我喜欢猫', quote='我喜欢猫', kind='preference', tags=[], ts=2000,
        source_event_id='', attach_anchor_id=None, source_kind='chat_turn')
    assert new['status'] == 'pending'
    old_row = engine.store.get_memory('semantics', old['id'])
    assert old_row['status'] == 'pending'
    assert old_row['conflict_group'] == new['conflict_group']
    assert engine.recall('猫')['semantics'] == []

    resolved = engine.resolve_conflict(new['conflict_group'], new['id'])
    assert resolved['ok'] is True
    assert engine.store.get_memory('semantics', old['id'])['status'] == 'archived'
    assert engine.store.get_memory('semantics', new['id'])['status'] == 'active'


def test_same_preference_reconfirms_instead_of_duplicating(engine):
    first = engine.store.add_semantic(
        '我喜欢喝美式', quote='我喜欢喝美式', kind='preference')
    again = engine._add_or_confirm_semantic(
        '我喜欢喝美式', quote='我喜欢喝美式', kind='preference', tags=[], ts=2000,
        source_event_id='', attach_anchor_id=None, source_kind='chat_turn')
    assert again['id'] == first['id']
    assert engine.store.stats()['semantics'] == 1
    assert engine.store.get_memory('semantics', first['id'])['confirm_count'] == 1


def test_same_preference_with_modifier_reconfirms(engine):
    first = engine.store.add_semantic(
        '我喜欢猫', quote='我喜欢猫', kind='preference')
    again = engine._add_or_confirm_semantic(
        '我特别喜欢猫', quote='我特别喜欢猫', kind='preference', tags=[], ts=2000,
        source_event_id='', attach_anchor_id=None, source_kind='chat_turn')
    assert again['id'] == first['id']
    assert engine.store.stats()['semantics'] == 1
    assert engine.store.get_memory('semantics', first['id'])['confirm_count'] == 1


def test_dislike_and_like_of_same_object_conflict(engine):
    old = engine.store.add_semantic(
        '我喜欢猫', quote='我喜欢猫', kind='preference')
    new = engine._add_or_confirm_semantic(
        '我讨厌猫', quote='我讨厌猫', kind='preference', tags=[], ts=2000,
        source_event_id='', attach_anchor_id=None, source_kind='chat_turn')
    assert new['status'] == 'pending'
    assert engine.store.get_memory('semantics', old['id'])['status'] == 'pending'


def test_different_decision_target_is_pending(engine):
    old = engine.store.add_semantic(
        '我决定去北京', quote='我决定去北京', kind='decision')
    new = engine._add_or_confirm_semantic(
        '我决定去上海', quote='我决定去上海', kind='decision', tags=[], ts=2000,
        source_event_id='', attach_anchor_id=None, source_kind='chat_turn')
    assert new['status'] == 'pending'
    assert engine.store.get_memory('semantics', old['id'])['status'] == 'pending'


def test_ui_preview_does_not_bump_recall_count(engine):
    anchor = engine.store.add_anchor(
        summary='猫毛过敏要戴口罩', quote='猫毛过敏要戴口罩', kind='fact', score=0.5)
    engine.recall('猫毛 过敏', count_as_recall=False)
    assert engine.store.get_anchor(anchor['id'])['recall_count'] == 0
    engine.recall('猫毛 过敏')
    assert engine.store.get_anchor(anchor['id'])['recall_count'] == 1


def test_event_and_preference_have_different_sleep_horizons(engine):
    event = engine.store.add_anchor(
        summary='一次普通事件', quote='一次普通事件', kind='event', score=0.5)
    preference = engine.store.add_semantic(
        '我喜欢喝美式', quote='我喜欢喝美式', kind='preference')
    now = int(event['created_at'])
    engine.apply_lifecycle(now=now + 91 * 86400)
    assert engine.store.get_anchor(event['id'])['status'] == 'dormant'
    assert engine.store.get_memory('semantics', preference['id'])['status'] == 'active'
    engine.apply_lifecycle(now=now + 181 * 86400)
    assert engine.store.get_memory('semantics', preference['id'])['status'] == 'dormant'


def test_pinned_memory_does_not_decay_or_sleep(engine):
    sem = engine.store.add_semantic(
        '我对猫毛过敏', quote='我对猫毛过敏', kind='preference')
    engine.store.set_pinned('semantics', sem['id'], True)
    now = int(engine.store.get_memory('semantics', sem['id'])['created_at'])
    engine.apply_lifecycle(now=now + 1000 * 86400)
    item = engine.store.get_memory('semantics', sem['id'])
    assert item['status'] == 'active'
    decorated = next(row for row in engine.list_memories() if row['id'] == sem['id'])
    assert decorated['effective_score'] == 1.0
    assert decorated['next_sleep_days'] is None


def test_confirm_wakes_dormant_memory(engine):
    sem = engine.store.add_semantic(
        '我喜欢喝美式', quote='我喜欢喝美式', kind='preference')
    row = engine.store.get_memory('semantics', sem['id'])
    engine.apply_lifecycle(now=int(row['created_at']) + 181 * 86400)
    assert engine.store.get_memory('semantics', sem['id'])['status'] == 'dormant'
    engine.confirm('semantics', sem['id'])
    assert engine.store.get_memory('semantics', sem['id'])['status'] == 'active'


def test_pending_conflict_cannot_be_confirmed_directly(engine):
    old = engine.store.add_semantic(
        '我不喜欢猫', quote='我不喜欢猫', kind='preference')
    new = engine._add_or_confirm_semantic(
        '我喜欢猫', quote='我喜欢猫', kind='preference', tags=[], ts=2000,
        source_event_id='', attach_anchor_id=None, source_kind='chat_turn')
    assert engine.confirm('semantics', old['id'])['reason'] == 'resolve_conflict_first'
    assert engine.pin('semantics', new['id'], True)['reason'] == 'resolve_conflict_first'


def test_conflict_creation_can_be_undone(engine):
    old = engine.store.add_semantic(
        '我不喜欢猫', quote='我不喜欢猫', kind='preference')
    new = engine._add_or_confirm_semantic(
        '我喜欢猫', quote='我喜欢猫', kind='preference', tags=[], ts=2000,
        source_event_id='', attach_anchor_id=None, source_kind='chat_turn')
    assert engine.undo()['ok'] is True
    assert engine.store.get_memory('semantics', old['id'])['status'] == 'active'
    assert engine.store.get_memory('semantics', new['id'])['status'] == 'archived'


def test_correction_can_be_undone(engine):
    sem = engine.store.add_semantic(
        '我喜欢喝美式', quote='我喜欢喝美式', kind='preference')
    engine.correct('semantics', sem['id'], content='我喜欢喝拿铁')
    assert engine.store.get_memory('semantics', sem['id'])['content'] == '我喜欢喝拿铁'
    assert engine.store.get_memory('semantics', sem['id'])['quote'] == '我喜欢喝美式'
    result = engine.undo()
    assert result['ok'] is True
    assert engine.store.get_memory('semantics', sem['id'])['content'] == '我喜欢喝美式'


def test_correcting_dormant_memory_wakes_it(engine):
    sem = engine.store.add_semantic(
        '我喜欢喝美式', quote='我喜欢喝美式', kind='preference')
    row = engine.store.get_memory('semantics', sem['id'])
    engine.apply_lifecycle(now=int(row['created_at']) + 181 * 86400)
    assert engine.store.get_memory('semantics', sem['id'])['status'] == 'dormant'
    engine.correct('semantics', sem['id'], content='我喜欢喝拿铁')
    item = engine.store.get_memory('semantics', sem['id'])
    assert item['status'] == 'active'
    assert item['content'] == '我喜欢喝拿铁'


def test_archived_and_dormant_not_recalled(engine):
    active = engine.store.add_anchor(
        summary='猫毛过敏要戴口罩', quote='猫毛过敏要戴口罩', kind='fact', score=0.5)
    dormant = engine.store.add_anchor(
        summary='猫毛过敏旧记录', quote='猫毛过敏旧记录', kind='fact', score=0.5)
    engine.store.set_memory_state('anchors', dormant['id'], status='dormant')
    archived = engine.store.add_anchor(
        summary='猫毛过敏已移除', quote='猫毛过敏已移除', kind='fact', score=0.5)
    engine.store.archive_memory('anchors', archived['id'])
    result = engine.recall('猫毛 过敏')
    ids = [a['id'] for a in result['anchors']]
    assert active['id'] in ids
    assert dormant['id'] not in ids
    assert archived['id'] not in ids


def test_old_database_is_migrated_with_evidence_backfill(tmp_path):
    import sqlite3

    db = tmp_path / 'memory.db'
    conn = sqlite3.connect(db)
    conn.executescript(
        'CREATE TABLE anchors (id TEXT PRIMARY KEY, ts INTEGER NOT NULL, '
        "time_label TEXT NOT NULL DEFAULT '', summary TEXT NOT NULL, "
        'emotion REAL NOT NULL DEFAULT 0.0, salience TEXT NOT NULL DEFAULT "warm", '
        'prev_id TEXT, next_id TEXT, tags TEXT NOT NULL DEFAULT "", '
        'recall_count INTEGER NOT NULL DEFAULT 0, last_recalled INTEGER NOT NULL DEFAULT 0, '
        'created_at INTEGER NOT NULL);'
        'CREATE TABLE semantics (id TEXT PRIMARY KEY, content TEXT NOT NULL, '
        "tags TEXT NOT NULL DEFAULT '', attach_anchor_id TEXT, created_at INTEGER NOT NULL);"
        'INSERT INTO anchors(id, ts, summary, created_at) '
        "VALUES('epi_old', 100, '旧事件原话', 100);"
        'INSERT INTO semantics(id, content, created_at) '
        "VALUES('sem_old', '旧偏好', 100);")
    conn.commit()
    conn.close()

    eng = MemoryEngine(str(db))
    anchor = eng.store.get_anchor('epi_old')
    sem = eng.store.get_memory('semantics', 'sem_old')
    assert anchor['quote'] == '旧事件原话'
    assert anchor['status'] == 'active'
    assert anchor['kind'] == 'event'
    assert anchor['pinned'] == 0
    assert sem['quote'] == '旧偏好'
    assert sem['status'] == 'active'
    assert sem['kind'] == 'fact'

