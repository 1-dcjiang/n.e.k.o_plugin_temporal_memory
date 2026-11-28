"""A1755 增量复审回归：假设误判、阈值、全量导出/清除、工作记忆邻接、
迁移备份静默失败、撤销记录、预算裁剪与固定条目不衰减。"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
import temporal_memory.engine as engine_module
import temporal_memory.memory_store as memory_store
from temporal_memory import scoring
from temporal_memory.engine import MemoryEngine, _is_sensitive
from temporal_memory.retrieval import _fit_budget, _rank_anchors


def _engine(tmp_path, settings=None):
    return MemoryEngine(str(tmp_path / 'memory.db'), settings or {})


def _revision_count(store) -> int:
    with store._lock, store._session() as conn:
        return int(conn.execute('SELECT COUNT(*) FROM revisions').fetchone()[0])


# ---------------------------------------------------------------- A1


def test_hypothesis_false_positives_are_narrowed():
    assert not scoring.is_hypothesis('我对花生过敏，如果我是在外面吃饭请提醒我')
    assert not scoring.is_hypothesis('如果你是在写代码，请用 4 空格')
    assert not scoring.is_hypothesis('I suppose this works')
    assert not scoring.is_hypothesis('假如我明天考试，记得提醒我带准考证')
    assert scoring.is_hypothesis('如果我是你，我会先睡一觉')
    assert scoring.is_hypothesis('假设一个角色：你当店长')
    assert scoring.is_hypothesis('what if we lived by the sea')


def test_factual_preference_survives_a_trailing_condition(tmp_path):
    engine = _engine(tmp_path)
    result = engine.observe('我对花生过敏，如果我是在外面吃饭请提醒我', ts=1000)
    assert result['hypothesis'] is False
    semantics = engine.store.all_memories('semantics', 10)
    assert any('花生' in (item.get('content') or '') for item in semantics)
    assert any(item.get('kind') == 'preference' for item in semantics)


def test_hypothesis_threshold_uses_raw_score(tmp_path):
    scored = scoring.score_turn('假设我是一只猫')
    assert 'hypothesis' in scored.signals
    assert scored.score >= 0.3
    assert scored.raw_score == 0.0
    engine = _engine(tmp_path)
    result = engine.observe('假设我是一只猫', ts=1000)
    assert result['hypothesis'] is True
    assert result['anchor'] is None
    assert engine.store.all_memories('anchors', 10, statuses=('hypothesis',)) == []


def test_hypothesis_capacity_cleanup_happens_after_transaction(tmp_path, monkeypatch):
    engine = _engine(tmp_path, {'anchor_threshold': 0.0})
    monkeypatch.setattr(engine_module, 'MAX_HYPOTHESES', 2)
    purged: list[bool] = []
    monkeypatch.setattr(engine.store, 'purge_delete_residue',
                        lambda: purged.append(True))
    for index in range(3):
        engine.observe(f'假设我是猫版本 {index}', ts=1000 + index)
    assert len(engine.store.all_memories(
        'anchors', limit=None, statuses=('hypothesis',))) == 2
    assert purged and engine._residue_purge_pending is False


# ---------------------------------------------------------------- A4 / A7


def test_sensitive_check_reads_all_text_fields():
    assert _is_sensitive({'quote': '今天聊得很开心', 'summary': '生日计划'}, 'anchors')
    assert _is_sensitive({'content': '普通正文', 'summary': '结婚纪念日'}, 'semantics')


def test_working_memory_filters_assistant_reply_after_sensitive_user_turn(tmp_path):
    engine = _engine(tmp_path)
    engine.observe('我的生日是 3 月 5 日', '好的，我记住了', ts=1000)
    exported = engine.export()
    assert exported['working_memory'] == []
    assert exported['redacted_sensitive'] >= 2
    cleared = engine.clear_sensitive()
    assert cleared['working'] == 2


def test_export_and_clear_do_not_stop_at_capacity_probe(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    for index in range(4):
        engine.store.add_anchor(
            summary=f'普通记录 {index}', quote=f'普通记录 {index}',
            ts=1000 + index, score=0.1, status='active', kind='event')

    engine2 = _engine(tmp_path / 'second')
    for index in range(4):
        engine2.store.add_anchor(
            summary=f'生日记录 {index}', quote=f'生日记录 {index}',
            ts=2000 + index, score=0.1, status='active', kind='event')

    # 数据量必须真正越过常量，否则旧实现 limit=MAX_TOTAL_MEMORIES+1 也能通过。
    # 常量在 memory_store，不在 engine；补丁目标必须正确。
    monkeypatch.setattr(memory_store, 'MAX_TOTAL_MEMORIES', 2)
    exported = engine.export(max_rows=10)
    assert len(exported['anchors']) == 4
    assert exported['truncated']['anchors'] is False

    cleared = engine2.clear_sensitive()
    assert cleared['anchors'] == 4


def test_confirm_hypothesis_writes_one_revision_and_undo_restores(tmp_path):
    engine = _engine(tmp_path, {'anchor_threshold': 0.0})
    anchor = engine.observe('假设我喜欢猫', ts=1000)['anchor']
    before = _revision_count(engine.store)
    confirmed = engine.confirm('anchors', anchor['id'])
    assert confirmed['ok'] is True
    assert confirmed['item']['status'] == 'active'
    assert confirmed['item']['kind'] == 'preference'
    assert _revision_count(engine.store) == before + 1
    engine.undo()
    restored = engine.store.get_memory('anchors', anchor['id'])
    assert restored['status'] == 'hypothesis'
    assert restored['kind'] == 'hypothesis'


def test_backup_cleanup_handles_special_paths_and_corrupt_files(tmp_path):
    from temporal_memory import TemporalMemoryPlugin

    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)
    data_dir = Path(plugin._db_path()).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    sensitive = data_dir / 'birth#day %.db.migrated'
    conn = sqlite3.connect(str(sensitive))
    conn.execute('CREATE TABLE anchors (quote TEXT, summary TEXT)')
    conn.execute('INSERT INTO anchors VALUES (?, ?)', ('今天是我生日', '生日'))
    conn.commit()
    conn.close()
    (data_dir / 'broken.db.migrated').write_text('not sqlite', encoding='utf-8')
    stats = plugin._purge_sensitive_migration_backups()
    assert stats == {'removed': 2, 'unreadable': 1, 'failed': 0}
    assert not sensitive.exists()
    assert not (data_dir / 'broken.db.migrated').exists()


def test_hypothesis_retention_uses_created_at_not_model_ts(tmp_path):
    engine = _engine(tmp_path, {'anchor_threshold': 0.0,
                                'hypothesis_retention_days': 14})
    engine.observe('假设我是一只猫', ts=1000)
    decorated = engine.list_memories(10, statuses=('hypothesis',))[0]
    assert decorated['delete_in_days'] > 13.9


def test_clear_sensitive_covers_unloaded_character_databases(tmp_path):
    import asyncio

    from temporal_memory import TemporalMemoryPlugin

    class FakeConfig:
        def __init__(self):
            self.data = {}

        async def dump(self, timeout=None):
            return dict(self.data)

        async def update(self, payload, timeout=None):
            self.data.update(payload)
            return dict(self.data)

    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)
    plugin.config = FakeConfig()
    data_dir = Path(plugin._db_path()).parent
    data_dir.mkdir(parents=True, exist_ok=True)
    backup = data_dir / 'memory-birth.db.migrated'
    conn = sqlite3.connect(str(backup))
    conn.execute('CREATE TABLE anchors (quote TEXT, summary TEXT)')
    conn.execute('INSERT INTO anchors VALUES (?, ?)', ('今天是我生日', '生日'))
    conn.commit()
    conn.close()
    for character in ('甲', '乙'):
        engine = MemoryEngine(plugin._db_path(character))
        engine.remember('我的生日是 3 月 5 日', ts=1000)
        engine.store.close()
    cleared = asyncio.run(plugin.clear_sensitive_memories(confirmed=True))
    assert cleared.ok is True
    assert cleared.value['databases'] == 2
    assert cleared.value['anchors'] + cleared.value['semantics'] >= 2
    assert cleared.value['backups'] == 1
    assert not backup.exists()


def test_sensitive_clear_i18n_placeholders_are_complete():
    import json

    root = Path(__file__).resolve().parents[1] / 'i18n'
    for name in ('zh-CN.json', 'en.json'):
        data = json.loads((root / name).read_text(encoding='utf-8'))
        text = data['ui.notice.clear_sensitive_done']
        assert '{{anchors}}' in text and '{{semantics}}' in text
        assert '{{working}}' in text and '{{backups}}' in text


# ---------------------------------------------------------------- retrieval


def test_fit_budget_drops_tail_instead_of_skipping_to_shorter_lines():
    out = _fit_budget(['A' * 80, 'short'], 40)
    assert out.startswith('A')
    assert len(out) <= 40
    assert 'short' not in out


def test_pinned_anchor_does_not_decay_in_ranking():
    now = 100_000
    old_pinned = {'id': 'old', 'ts': now - 365 * 86400, 'score': 0.5,
                  'pinned': 1, 'recall_count': 0}
    new_weak = {'id': 'new', 'ts': now, 'score': 0.3,
                'pinned': 0, 'recall_count': 0}
    ranked = _rank_anchors([old_pinned, new_weak], now=now)
    assert ranked[0]['id'] == 'old'


def test_consolidate_archives_only_unpinned_active_at_update_time(tmp_path):
    engine = _engine(tmp_path, {'anchor_threshold': 0.0})
    first = engine.store.add_anchor(
        summary='我喜欢喝美式', quote='我喜欢喝美式', ts=1000,
        score=0.8, status='active', kind='preference')
    second = engine.store.add_anchor(
        summary='我喜欢喝美式', quote='我喜欢喝美式', ts=1001,
        score=0.2, status='active', kind='preference', pinned=True)
    engine.consolidate(now=1002)
    assert engine.store.get_memory('anchors', first['id'])['status'] == 'archived'
    assert engine.store.get_memory('anchors', second['id'])['status'] == 'active'
    result = engine.store.set_memory_state(
        'anchors', second['id'], status='archived',
        require_unpinned_active=True)
    assert result == {'ok': False, 'reason': 'not_active_unpinned'}


@pytest.mark.skipif(not Path(__file__).with_name('test_real_sdk.py').exists(),
                    reason='real SDK test not present')
def test_real_sdk_test_uses_import_alias():
    source = Path(__file__).with_name('test_real_sdk.py').read_text(encoding='utf-8')
    assert 'TM_PLUGIN_ROOT' in source
    assert 'submodule_search_locations' in source
