"""A1825 增量审核回归：INLINE/事实顺序、时间白名单、M3 收窄、
备份预览与 >5001 行全量路径。"""

from __future__ import annotations

import sqlite3

from temporal_memory import TemporalMemoryPlugin, scoring
from temporal_memory.engine import MemoryEngine, is_sensitive_text


def _engine(tmp_path):
    return MemoryEngine(str(tmp_path / 'memory.db'), {})


class _FakeConfig:
    def __init__(self):
        self.data = {}

    async def dump(self, timeout: float | None = None):
        return dict(self.data)

    async def update(self, payload, timeout: float | None = None):
        self.data.update(payload)
        return dict(self.data)


def _plugin(tmp_path):
    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)
    plugin.config = _FakeConfig()
    return plugin


def _bulk_anchors(store, count: int, *, text: str, status: str = 'active',
                  day: int = 0) -> None:
    rows = [(f'bulk-{i}', day + i, text, text, status, day + i)
            for i in range(count)]
    with store._lock, store._session() as conn:
        conn.executemany(
            'INSERT OR IGNORE INTO anchors'
            '(id, ts, summary, quote, status, created_at) '
            'VALUES (?, ?, ?, ?, ?, ?)', rows)
        conn.commit()


# ---------------------------------------------------------------- M1


def test_inline_hypothesis_does_not_override_complete_fact_clause():
    assert not scoring.is_hypothesis('我对花生过敏，假如你做饭请注意')
    assert not scoring.is_hypothesis('我的版本是 3.2，假如升级失败请回滚')
    assert scoring.is_hypothesis('那假设我是医生')
    assert scoring.is_hypothesis('请假设我是店长')
    assert scoring.is_hypothesis('咱们来玩角色扮演')
    assert scoring.is_hypothesis('好的！假设我是医生')


def test_time_whitelist_requires_two_char_time_words():
    assert not scoring.is_hypothesis('如果我是晚上问你，请简短回答')
    assert not scoring.is_hypothesis('如果我是明天开会，请提醒我')
    assert not scoring.is_hypothesis('假如我是晚上问你，请简短回答')
    assert scoring.is_hypothesis('如果我是上司，你会怎么汇报')
    assert scoring.is_hypothesis('如果我是周杰伦')
    assert scoring.is_hypothesis('如果我是明星')
    assert scoring.is_hypothesis('如果我是会计')


def test_jia_ru_is_condition_unless_it_has_first_person_role():
    assert not scoring.is_hypothesis('假如明天下雨，请提醒我带伞')
    assert scoring.is_hypothesis('假如我是一个有钱人')


# ---------------------------------------------------------------- M3


def test_m3_context_words_no_longer_match_project_or_dependency():
    assert not is_sensitive_text('我们下周的项目里程碑')
    assert not is_sensitive_text('依赖关系里程碑')
    assert not is_sensitive_text('I propose we set the next milestone')
    assert not is_sensitive_text('第一次见这个报错，印象很深')
    assert is_sensitive_text('我们感情的里程碑')
    assert is_sensitive_text('第一次见他我心动了')


def test_m3_english_and_chinese_substring_edges():
    assert not is_sensitive_text('birthday attack')
    assert not is_sensitive_text('Windows 10 Anniversary Update')
    assert not is_sensitive_text('广告白名单')
    assert not is_sensitive_text('公告白名单')
    assert not is_sensitive_text('公司周年庆')
    assert is_sensitive_text('birthday party')
    assert is_sensitive_text('结婚十周年')


def test_preview_marks_emotion_only_and_pinned(tmp_path):
    engine = _engine(tmp_path)
    engine.store.add_anchor(
        summary='插件又崩溃了', quote='插件又崩溃了', ts=1000, score=0.1,
        emotion=0.9, pinned=True, status='active', kind='event')
    preview = engine.preview_sensitive()
    assert preview['total'] == 1
    sample = preview['samples'][0]
    assert sample['reason'] == 'emotion'
    assert sample['pinned'] is True


def test_preview_counts_migration_backups_without_deleting(tmp_path):
    plugin = _plugin(tmp_path)
    backup = tmp_path / 'memory.db.migrated'
    conn = sqlite3.connect(backup)
    conn.execute('CREATE TABLE anchors(summary TEXT)')
    conn.execute("INSERT INTO anchors VALUES('生日记录')")
    conn.commit()
    conn.close()
    stats = plugin._scan_sensitive_migration_backups()
    assert stats['backups'] == 1
    assert backup.exists()


# ---------------------------------------------------------------- M2 full path


def test_export_and_clear_really_scan_more_than_5001_rows(tmp_path):
    engine = _engine(tmp_path)
    _bulk_anchors(engine.store, 5002, text='普通记录')
    exported = engine.export(max_rows=10000)
    assert len(exported['anchors']) == 5002
    assert exported['truncated']['anchors'] is False

    birthday = _engine(tmp_path / 'birthday')
    _bulk_anchors(birthday.store, 5002, text='生日记录')
    cleared = birthday.clear_sensitive()
    assert cleared['anchors'] == 5002


def test_consolidate_really_scans_more_than_5001_hypotheses(tmp_path):
    engine = _engine(tmp_path)
    _bulk_anchors(engine.store, 5002, text='过期的假设', status='hypothesis',
                  day=1000)
    counts = engine.consolidate(now=10 ** 9)
    assert counts['hypotheses'] == 5002
