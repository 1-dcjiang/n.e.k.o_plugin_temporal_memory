"""A1809 增量深审回归：假设绕过、空转测试、敏感词误伤、批量删除收尾、
固定假设行、备份扫描口径。"""

from __future__ import annotations

import sqlite3

from temporal_memory import scoring
from temporal_memory.engine import MemoryEngine, is_sensitive_text


def _engine(tmp_path):
    return MemoryEngine(str(tmp_path / 'memory.db'), {})


# ---------------------------------------------------------------- M1 / L2-L4


def test_hypothesis_prefix_wins_over_first_person_fact_exemption():
    assert scoring.is_hypothesis('假设我是店长，如果顾客投诉你怎么办')
    assert scoring.is_hypothesis('我们来玩角色扮演，我是医生，如果你生病了怎么办')
    assert not scoring.is_hypothesis('我对花生过敏，如果我是在外面吃饭请提醒我')
    assert not scoring.is_hypothesis('这个功能主要是要是没网就会报错')


def test_role_pattern_does_not_match_time_or_demo_substrings():
    assert not scoring.is_hypothesis('如果你当时提醒我就好了')
    assert not scoring.is_hypothesis('如果你演示一下这个参数')


def test_first_person_hypothesis_skips_time_condition_rules():
    assert not scoring.is_hypothesis('如果我是晚上问你，请简短回答')
    assert scoring.is_hypothesis('如果我是店长，你会怎么接待我')
    assert scoring.is_hypothesis('我不记得了，假设我是医生')


def test_hypothesis_raw_score_keeps_one_weight_signal():
    scored = scoring.score_turn('假设我第一次用这个')
    assert 'hypothesis' in scored.signals
    assert 'knowledge' in scored.signals
    assert scored.raw_score >= 0.3


# ---------------------------------------------------------------- M3


def test_sensitive_terms_no_longer_match_dev_or_business_context():
    assert not is_sensitive_text('第一次见这个报错')
    assert not is_sensitive_text('项目里程碑')
    assert not is_sensitive_text('grant proposal')
    assert not is_sensitive_text('confession in the config file')
    assert is_sensitive_text('我们感情的里程碑')
    assert is_sensitive_text('第一次见他我心动了')
    assert is_sensitive_text('生日')


def test_preview_reports_matches_without_deleting(tmp_path):
    engine = _engine(tmp_path)
    engine.store.add_anchor(summary='生日记录', quote='生日记录', ts=1000,
                            score=0.1, status='active', kind='event')
    engine.store.add_anchor(summary='普通记录', quote='普通记录', ts=1001,
                            score=0.1, status='active', kind='event')
    preview = engine.preview_sensitive()
    assert preview['total'] == 1
    assert preview['anchors'] == 1
    assert preview['samples'][0]['text'] == '生日记录'
    assert len(engine.store.all_memories('anchors', limit=None)) == 2


# ---------------------------------------------------------------- L1 / L6


def test_clear_sensitive_purges_residue_once(tmp_path, monkeypatch):
    engine = _engine(tmp_path)
    for index in range(2):
        engine.store.add_anchor(summary=f'生日记录 {index}',
                                quote=f'生日记录 {index}', ts=1000 + index,
                                score=0.1, status='active', kind='event')
    engine.store.add_anchor(summary='普通记录', quote='普通记录', ts=2000,
                            score=0.1, status='active', kind='event')
    engine.store.add_turn('user', '我的生日是 5 月 1 日', ts=3000)
    calls: list[int] = []
    monkeypatch.setattr(engine.store, 'purge_delete_residue',
                        lambda: calls.append(1))
    cleared = engine.clear_sensitive()
    assert cleared['anchors'] == 2
    assert cleared['working'] == 1
    assert calls == [1]


def test_pinning_hypothesis_reclassifies_kind(tmp_path):
    engine = _engine(tmp_path)
    anchor = engine.store.add_anchor(
        summary='假设我是医生', quote='假设我是医生', ts=1000, score=0.6,
        status='hypothesis', kind='hypothesis')
    assert anchor['status'] == 'hypothesis'
    pinned = engine.pin('anchors', anchor['id'], True)
    assert pinned['ok'] is True
    item = engine.store.get_memory('anchors', anchor['id'])
    assert item['status'] == 'active'
    assert item['pinned'] == 1
    assert item['kind'] != 'hypothesis'


# ---------------------------------------------------------------- L5


def test_backup_scan_uses_actual_columns_and_tags(tmp_path):
    from temporal_memory import _backup_has_sensitive

    path = tmp_path / 'old.db'
    conn = sqlite3.connect(path)
    conn.execute('CREATE TABLE anchors(summary TEXT, tags TEXT, emotion REAL)')
    conn.execute('CREATE TABLE semantics(content TEXT, quote TEXT)')
    conn.execute("INSERT INTO anchors VALUES('普通记录', '普通', 0.1)")
    conn.execute("INSERT INTO semantics VALUES('普通记录', '生日')")
    conn.commit()
    conn.close()
    assert _backup_has_sensitive(str(path)) is True

    path2 = tmp_path / 'tags.db'
    conn = sqlite3.connect(path2)
    conn.execute('CREATE TABLE anchors(summary TEXT, tags TEXT)')
    conn.execute("INSERT INTO anchors VALUES('普通记录', '生日,旅行')")
    conn.commit()
    conn.close()
    assert _backup_has_sensitive(str(path2)) is True
