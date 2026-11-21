"""0.5.16：面板读取与统计的逐库容错、维护错误可见、secure_delete 口径。

1) `_aggregate_stats` 单库抛错只跳过它，坏库名进 `stats_errors`；
2) `_settings_snapshot` 单库抛错只跳过它，坏库名进 `read_errors`，其余角色照常显示；
3) `_maintenance_errors` 进快照与 `status`，面板可见；
4) secure_delete 文档不再写“零覆盖”，WAL/导出/备份的明文边界写清。
"""

from __future__ import annotations

import json
from pathlib import Path

from temporal_memory import TemporalMemoryPlugin

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / 'ui' / 'settings.tsx'
LOCALES = {'zh-CN': ROOT / 'i18n' / 'zh-CN.json', 'en': ROOT / 'i18n' / 'en.json'}


class BadStore:
    db_path = 'bad.db'

    def stats(self):
        raise RuntimeError('disk I/O error')

    def recent_turns(self, n):
        raise RuntimeError('disk I/O error')

    def pending_conflicts(self):
        raise RuntimeError('disk I/O error')

    def orphan_pending(self, n):
        raise RuntimeError('disk I/O error')


class BadEngine:
    store = BadStore()

    def list_memories(self, *args, **kwargs):
        raise RuntimeError('database is locked')


class GoodStore:
    db_path = 'good.db'

    def stats(self):
        return {'anchors': 3, 'semantics': 2, 'edges': 1, 'working_turns': 0,
                'turn_no': 0, 'fts_mode': 'trigram', 'pending_conflicts': 0,
                'tiers': {'hot': 1}, 'statuses': {'active': 5},
                'unique_index_skipped': []}

    def recent_turns(self, n):
        return []

    def pending_conflicts(self):
        return []

    def orphan_pending(self, n):
        return []


class GoodEngine:
    store = GoodStore()

    def list_memories(self, limit: int, statuses=()):
        if 'archived' in statuses:
            return []
        return [{'id': 'a1', 'status': 'active', 'ts': 1, 'summary': '好库的一条',
                 'quote': '原话', 'score': 0}]


def _plugin(tmp_path):
    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)
    plugin._engines = {'坏库': BadEngine(), '好库': GoodEngine()}
    return plugin


def test_aggregate_stats_skips_broken_engine(tmp_path):
    plugin = _plugin(tmp_path)
    stats = plugin._aggregate_stats()
    assert stats['anchors'] == 3
    assert stats['semantics'] == 2
    assert stats['stats_errors'] == ['坏库']


def test_aggregate_stats_all_broken_returns_zeros(tmp_path):
    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)
    plugin._engines = {'坏库': BadEngine()}
    stats = plugin._aggregate_stats()
    assert stats['anchors'] == 0
    assert stats['stats_errors'] == ['坏库']


def test_snapshot_skips_broken_engine_but_keeps_others(tmp_path):
    plugin = _plugin(tmp_path)
    snapshot = plugin._settings_snapshot()
    assert snapshot['read_errors'] == ['坏库']
    assert [item['summary'] for item in snapshot['memories']] == ['好库的一条']
    assert snapshot['status']['stats']['stats_errors'] == ['坏库']


def test_snapshot_rolls_back_partial_rows_from_broken_engine(tmp_path):
    """坏库在追加到一半时抛错，已经加进去的半截行要被撤回。"""
    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)

    class HalfEngine:
        store = GoodStore()

        def list_memories(self, limit: int, statuses=()):
            if 'archived' in statuses:
                raise RuntimeError('database is locked')
            return [{'id': 'half', 'status': 'active', 'ts': 1, 'summary': '半截'}]

    plugin._engines = {'半截库': HalfEngine()}
    snapshot = plugin._settings_snapshot()
    assert snapshot['read_errors'] == ['半截库']
    assert snapshot['memories'] == []


def test_maintenance_errors_visible_in_snapshot(tmp_path):
    plugin = _plugin(tmp_path)
    plugin._maintenance_errors['坏库'] = 'boom'
    snapshot = plugin._settings_snapshot()
    assert snapshot['maintenance_errors'] == {'坏库': 'boom'}
    assert snapshot['status']['maintenance_errors'] == {'坏库': 'boom'}


def test_panel_and_locales_cover_status_error_keys():
    text = PANEL.read_text(encoding='utf-8')
    for key in ('ui.status.read_errors', 'ui.status.maintenance_errors'):
        assert key in text
        for path in LOCALES.values():
            data = json.loads(path.read_text(encoding='utf-8'))
            assert key in data and '{{count}}' in data[key]


def test_secure_delete_wording_drops_absolute_claim():
    for name in ('docs/guide.md', 'README.md'):
        text = (ROOT / name).read_text(encoding='utf-8')
        assert '零覆盖' not in text
    guide = (ROOT / 'docs' / 'guide.md').read_text(encoding='utf-8')
    assert 'WAL' in guide and '备份' in guide
