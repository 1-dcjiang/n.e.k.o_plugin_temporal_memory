"""A1351 安全性复核（0.5.14）挑出的四处轻项回归。

1) 面板工作记忆跟随 working_window，不再写死 8 行；
2) 启动维护与定时器同一口径，总开关关闭时不做沉降；
3) 删除联动弹窗给原文预览（面板侧，用 i18n 键锁定）；
4) character 显式传 null 落到默认库，不再遍历所有库误删同 id 行。
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

from temporal_memory import TemporalMemoryPlugin

ROOT = Path(__file__).resolve().parents[1]
PANEL = ROOT / 'ui' / 'settings.tsx'
LOCALES = {'zh-CN': ROOT / 'i18n' / 'zh-CN.json', 'en': ROOT / 'i18n' / 'en.json'}


class FakeConfig:
    def __init__(self):
        self.data = {}

    async def dump(self, timeout: float | None = None):
        return dict(self.data)

    async def update(self, payload, timeout: float | None = None):
        self.data.update(payload)
        return dict(self.data)


def _make_plugin(tmp_path):
    plugin = TemporalMemoryPlugin(ctx=None)
    plugin.storage_dir = str(tmp_path)
    plugin.config = FakeConfig()
    return plugin


# ---------------------------------------------------------------- 工作记忆窗口


def test_snapshot_working_memory_follows_window(tmp_path):
    """把 working_window 调大后，面板要能看到窗口里的全部行，而不是永远 8 行。"""
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.update_settings({'working_window': 20}))
    for index in range(20):
        asyncio.run(plugin.observe(user_text=f'第 {index} 轮用户发言'))
    snapshot = plugin._settings_snapshot()
    assert len(snapshot['working_memory']) == 20
    assert len(plugin._engines[''].store.recent_turns(50)) == 20


def test_snapshot_working_memory_defaults_to_eight(tmp_path):
    plugin = _make_plugin(tmp_path)
    for index in range(10):
        asyncio.run(plugin.observe(user_text=f'第 {index} 轮用户发言'))
    assert len(plugin._settings_snapshot()['working_memory']) == 8


def test_working_window_helper_clamps(tmp_path):
    plugin = _make_plugin(tmp_path)
    plugin._settings['working_window'] = 999
    assert plugin._working_window() == 50
    plugin._settings['working_window'] = 0
    assert plugin._working_window() == 0
    plugin._settings['working_window'] = 'bad'
    assert plugin._working_window() == 8


# ---------------------------------------------------------------- 启动维护与总开关


def test_startup_skips_maintenance_when_disabled(tmp_path):
    plugin = _make_plugin(tmp_path)
    plugin.config.data['settings'] = {'enabled': False}
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    calls = {'n': 0}
    real = engine.maintenance

    def counting():
        calls['n'] += 1
        return real()

    engine.maintenance = counting
    result = asyncio.run(plugin.on_startup())
    assert result.ok is True
    assert calls['n'] == 0
    assert plugin._ready is True


def test_startup_runs_maintenance_when_enabled(tmp_path):
    plugin = _make_plugin(tmp_path)
    plugin.config.data['settings'] = {'enabled': True}
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    calls = {'n': 0}
    real = engine.maintenance

    def counting():
        calls['n'] += 1
        return real()

    engine.maintenance = counting
    asyncio.run(plugin.on_startup())
    assert calls['n'] == 1


# ---------------------------------------------------------------- null 角色不遍历


def _two_engines(plugin):
    """手工装两个库：默认库 memory.db 与角色库 memory-乙.db 并存，id 都从 1 开始。"""
    from temporal_memory.engine import MemoryEngine

    default = MemoryEngine(plugin._db_path(''))
    role = MemoryEngine(plugin._db_path('乙'))
    plugin._engines[''] = default
    plugin._engines['乙'] = role
    return default, role


def test_delete_working_turn_null_character_hits_default_db_only(tmp_path):
    """显式传 null 等价于没传：只落默认库，不能遍历所有库删同 id 行。"""
    plugin = _make_plugin(tmp_path)
    default, role = _two_engines(plugin)
    default.observe('默认库的一句')
    role.observe('乙的一句')
    assert role.store.recent_turns(8)[0]['id'] == default.store.recent_turns(8)[0]['id']

    result = asyncio.run(plugin.delete_working_turn(
        id=default.store.recent_turns(8)[0]['id'], character=None, confirmed=True))
    assert result.ok is True
    assert default.store.recent_turns(8) == []
    assert [t['content'] for t in role.store.recent_turns(8)] == ['乙的一句']


def test_delete_memory_null_character_hits_default_db_only(tmp_path):
    plugin = _make_plugin(tmp_path)
    default, role = _two_engines(plugin)
    default_item = default.store.add_semantic('默认库的偏好', quote='我喜欢猫')
    role_item = role.store.add_semantic('角色库的偏好', quote='我也喜欢猫')

    result = asyncio.run(plugin.delete_memory(
        table='semantics', id=str(default_item['id']), character=None, confirmed=True))
    assert result.ok is True
    assert all(item['id'] != default_item['id']
               for item in default.store.recent_semantics())
    assert any(item['id'] == role_item['id']
               for item in role.store.recent_semantics())


def test_delete_candidates_never_returns_all_databases(tmp_path):
    """候选池按角色收敛：多库存在时也只给出那一个库的路径。"""
    plugin = _make_plugin(tmp_path)
    default, role = _two_engines(plugin)
    third = asyncio.run(plugin._ensure_engine('甲'))
    assert third is not None
    candidates = plugin._delete_candidates('甲', {})
    assert candidates == [plugin._db_path('甲')]
    assert plugin._delete_candidates('乙', {}) == [plugin._db_path('乙')]


# ---------------------------------------------------------------- 弹窗预览（i18n 锁定）


def test_working_hits_preview_key_exists_in_both_locales():
    for name, path in LOCALES.items():
        bundle = json.loads(path.read_text(encoding='utf-8'))
        assert 'ui.confirm.working_hits_line' in bundle, name
        assert '{{role}}' in bundle['ui.confirm.working_hits_line']


def test_panel_confirmation_lists_preview_lines():
    source = PANEL.read_text(encoding='utf-8')
    assert 'workingHitLines(hits)' in source
    assert 'hit.preview' in source
    assert 'ui.confirm.working_hits_line' in source
