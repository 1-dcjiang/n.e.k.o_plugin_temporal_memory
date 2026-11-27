"""A1752 深审回归：A1 假设误判、A2 加权召回、A3 摘要标签、A4 敏感范围、
A5 去重不动固定/撤销、A6 预算下限、A7 导出全量与确认后类型。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

from temporal_memory import retrieval, scoring
from temporal_memory.engine import MemoryEngine


def _engine(tmp_path, settings=None):
    return MemoryEngine(str(tmp_path / 'memory.db'), settings or {})


def _revision_count(store) -> int:
    with store._lock, store._session() as conn:
        return int(conn.execute('SELECT COUNT(*) FROM revisions').fetchone()[0])


# ---------------------------------------------------------------- A1


def test_explicit_memory_and_conditions_are_not_hypotheses():
    assert not scoring.is_hypothesis('记住，如果我说红色就停')
    assert not scoring.is_hypothesis('我对花生过敏，如果做饭注意')
    assert not scoring.is_hypothesis('设定一个明天的闹钟')
    assert not scoring.is_hypothesis('如果明天下雨就带伞')
    assert scoring.is_hypothesis('如果我是你，我会先睡一觉')
    assert scoring.is_hypothesis('假设一个角色：你当店长')


def test_explicit_memory_beats_hypothesis_prefix():
    assert not scoring.is_hypothesis('假设我是猫，记住这条别忘')
    assert scoring.is_explicit_memory('假设我是猫，记住这条别忘')


def test_alarm_command_is_anchored_not_hypothesis(tmp_path):
    engine = _engine(tmp_path)
    result = engine.observe('设定一个明天的闹钟', ts=1000)
    assert result['hypothesis'] is False
    assert result['anchor']['status'] == 'active'
    assert result['anchor']['quote'] == '设定一个明天的闹钟'


def test_low_score_hypothesis_is_not_written(tmp_path):
    engine = _engine(tmp_path, {'anchor_threshold': 0.6})
    result = engine.observe('假设我是一只猫', ts=1000)
    assert result['hypothesis'] is True
    assert result['anchor'] is None
    assert engine.store.all_memories('anchors', 10, statuses=('hypothesis',)) == []
    assert engine.list_memories(10, statuses=('hypothesis',)) == []


def test_hypothesis_does_not_spend_timeline_capacity(tmp_path):
    engine = _engine(tmp_path)
    normal = engine.remember('我养了一只猫叫团子', ts=1000)
    assert normal['anchor']['status'] == 'active'
    engine.observe('假设我是一只猫，你会怎么照顾我', ts=1001)
    assert engine.store.all_memories('anchors', 10, statuses=('hypothesis',))
    assert engine.store.all_memories('anchors', 10, statuses=('active',))


# ---------------------------------------------------------------- A2 / A3


def test_rank_prefers_score_over_raw_search_order():
    old_high = {'id': 'old_high', 'ts': 1000, 'score': 1.0, 'recall_count': 0}
    new_low = {'id': 'new_low', 'ts': 10_000, 'score': 0.05, 'recall_count': 0}
    ranked = retrieval._rank_anchors([new_low, old_high], now=10_000)
    assert ranked[0]['id'] == 'old_high'


def test_recall_pool_goes_beyond_max_anchors(tmp_path):
    engine = _engine(tmp_path, {'max_recall_anchors': 1,
                                'recall_char_budget': 4000,
                                'recall_token_budget': 8000})
    for index in range(7):
        engine.store.add_anchor(
            summary=f'眼药水记录 {index}', quote=f'眼药水记录 {index}',
            ts=1000 + index, score=0.05, status='active', kind='event')
    engine.store.add_anchor(
        summary='眼药水最重要的那条', quote='眼药水最重要的那条',
        ts=1006, score=1.0, status='active', kind='event')
    result = engine.recall('眼药水')
    assert len(result['anchors']) == 1
    assert result['anchors'][0]['score'] == 1.0


def test_summary_is_labelled_summary_not_user_quote(tmp_path):
    engine = _engine(tmp_path)
    engine.store.add_anchor(
        summary='用户: 我对猫毛过敏', quote='我对猫毛过敏',
        ts=1000, score=0.8, status='active', kind='preference')
    block = engine.recall('猫毛 过敏')['prompt_block']
    assert '｜摘要]' in block
    assert '｜用户原话]' not in block
    assert '用户原话：' in block


def test_summary_never_contains_assistant_reply(tmp_path):
    engine = _engine(tmp_path)
    engine.observe('记住，我对猫毛过敏，家里不能养猫', '好的，我会记住你对猫毛过敏',
                   ts=1000)
    anchors = engine.store.all_memories('anchors', 10)
    assert anchors
    assert all('我会记住' not in (item.get('summary') or '') for item in anchors)


# ---------------------------------------------------------------- A4


def test_birthday_without_tags_is_sensitive(tmp_path):
    engine = _engine(tmp_path)
    engine.remember('我的生日是 3 月 5 日', ts=1000)
    exported = engine.export()
    assert exported['redacted_sensitive'] >= 1
    assert not any('生日' in (item.get('quote') or '') for item in exported['anchors'])
    assert not any('生日' in (item.get('content') or '') for item in exported['semantics'])
    full = engine.export(include_sensitive=True)
    assert any(item.get('quote') == '我的生日是 3 月 5 日' for item in full['anchors'])


def test_export_filters_working_memory_and_clear_removes_it(tmp_path):
    engine = _engine(tmp_path)
    engine.observe('我的生日是 3 月 5 日', '祝你生日快乐', ts=1000)
    exported = engine.export()
    assert not any('生日' in (turn.get('content') or '')
                   for turn in exported['working_memory'])
    assert exported['redacted_sensitive'] >= 1
    cleared = engine.clear_sensitive()
    assert cleared['working'] >= 1
    assert not any('生日' in (turn.get('content') or '')
                   for turn in engine.store.all_working_turns())


def test_clear_sensitive_purges_only_sensitive_migration_backup(tmp_path):
    from temporal_memory import TemporalMemoryPlugin

    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)
    data_dir = Path(plugin._db_path()).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    for name, text in (('memory.db.migrated', '今天是我生日'),
                       ('memory-safe.db.migrated', '我养了一只猫')):
        conn = sqlite3.connect(str(data_dir / name))
        conn.execute('CREATE TABLE anchors (quote TEXT, summary TEXT)')
        conn.execute('INSERT INTO anchors VALUES (?, ?)', (text, text))
        conn.commit()
        conn.close()
    assert plugin._purge_sensitive_migration_backups() == 1
    assert not (data_dir / 'memory.db.migrated').exists()
    assert (data_dir / 'memory-safe.db.migrated').exists()


# ---------------------------------------------------------------- A5


def test_consolidate_keeps_pinned_even_with_lower_score(tmp_path):
    engine = _engine(tmp_path)
    pinned = engine.store.add_anchor(
        summary='我喜欢喝美式', quote='我喜欢喝美式', ts=1000,
        score=0.2, pinned=True, status='active', kind='preference')
    duplicate = engine.store.add_anchor(
        summary='我喜欢喝美式', quote='我喜欢喝美式', ts=1001,
        score=0.9, pinned=False, status='active', kind='preference')
    counts = engine.consolidate(now=1002)
    assert counts['anchors'] == 1
    assert engine.store.get_memory('anchors', pinned['id'])['status'] == 'active'
    assert engine.store.get_memory('anchors', duplicate['id'])['status'] == 'archived'


def test_consolidate_does_not_write_undo_revisions(tmp_path):
    engine = _engine(tmp_path)
    engine.store.add_anchor(
        summary='我喜欢喝美式', quote='我喜欢喝美式', ts=1000,
        score=0.9, status='active', kind='preference')
    engine.store.add_anchor(
        summary='我喜欢喝美式', quote='我喜欢喝美式', ts=1001,
        score=0.2, status='active', kind='preference')
    before = _revision_count(engine.store)
    engine.consolidate(now=1002)
    after = _revision_count(engine.store)
    assert after == before


# ---------------------------------------------------------------- A6 / A7


def test_char_budget_is_a_hard_cap(tmp_path):
    engine = _engine(tmp_path, {'recall_char_budget': 200,
                                'recall_token_budget': 240})
    for index in range(6):
        engine.remember(f'眼药水记录 {index} ' + '别揉眼睛' * 20, ts=1000 + index)
    result = engine.recall('眼药水')
    assert result['prompt_block'].strip()
    assert result['chars'] <= 200


def test_export_does_not_hide_plain_rows_behind_sensitive_rows(tmp_path):
    engine = _engine(tmp_path)
    engine.remember('今天是我生日，第一次一起过纪念日', ts=1002)
    engine.remember('我的生日是 3 月 5 日', ts=1001)
    engine.remember('我养了一只猫叫团子', ts=1000)
    exported = engine.export(max_rows=1)
    assert [item.get('quote') for item in exported['anchors']] == ['我养了一只猫叫团子']
    assert exported['truncated']['anchors'] is False


def test_confirm_hypothesis_changes_kind_and_becomes_recallable(tmp_path):
    engine = _engine(tmp_path)
    result = engine.observe('假设我是一只猫，你会怎么照顾我', ts=1000)
    anchor = result['anchor']
    assert anchor['kind'] == 'hypothesis'
    confirmed = engine.confirm('anchors', anchor['id'])
    assert confirmed['ok'] is True
    item = engine.store.get_memory('anchors', anchor['id'])
    assert item['status'] == 'active'
    assert item['kind'] != 'hypothesis'
    assert engine.recall('一只猫')['anchors']
