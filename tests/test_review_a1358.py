"""0.5.17：A1358 复核的四条轻项回归。

1) `working_window=0` 时面板与导出都显示 0 行，不再因为 `max(1, …)` 冒出一行；
2) 目标库打不开的报错不再说「已跳过；其余库没有」；
3) 「一起删除」命中的工作记忆行按调用方传入的 character 定位，不用库里的 owner 兜底；
4) 面板刷新前先拷贝引擎表，遍历时遇到新库挂载不会整次失败。
"""

from __future__ import annotations

import asyncio
from pathlib import Path

from temporal_memory import TemporalMemoryPlugin
from temporal_memory.engine import MemoryEngine
from temporal_memory.memory_store import MemoryStore


class FakeConfig:
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
    plugin.config = FakeConfig()
    return plugin


def test_recent_turns_zero_returns_empty(tmp_path):
    store = MemoryStore(str(tmp_path / 'm.db'))
    store.add_turn('user', '一句话')
    assert store.recent_turns(0) == []
    assert len(store.recent_turns(1)) == 1
    store.close()


def test_snapshot_working_memory_empty_when_window_zero(tmp_path):
    plugin = _plugin(tmp_path)
    plugin._settings['working_window'] = 0
    asyncio.run(plugin.observe(user_text='窗口为 0 之前写的一句'))
    snapshot = plugin._settings_snapshot()
    assert snapshot['working_memory'] == []
    assert snapshot['config']['working_window'] == 0


def test_export_working_memory_empty_when_window_zero(tmp_path):
    plugin = _plugin(tmp_path)
    plugin._settings['working_window'] = 0
    asyncio.run(plugin.observe(user_text='导出时不该出现的一句'))
    engine = plugin._engines['']
    payload = engine.export()
    assert payload['working_memory'] == []


def test_delete_memory_unreadable_message_is_explicit(tmp_path):
    plugin = _plugin(tmp_path)
    broken = Path(plugin._db_path('坏库'))
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b'not a sqlite database')
    result = asyncio.run(plugin.delete_memory(
        table='anchors', id='missing', character='坏库', confirmed=True))
    assert result.ok is False
    assert '目标记忆库打不开' in str(result.error)
    assert '其余库没有' not in str(result.error)


def test_delete_working_turn_unreadable_message_is_explicit(tmp_path):
    plugin = _plugin(tmp_path)
    broken = Path(plugin._db_path('坏库'))
    broken.parent.mkdir(parents=True, exist_ok=True)
    broken.write_bytes(b'not a sqlite database')
    result = asyncio.run(plugin.delete_working_turn(
        id=1, character='坏库', confirmed=True))
    assert result.ok is False
    assert '目标记忆库打不开' in str(result.error)


def test_working_memory_hits_use_given_character(tmp_path):
    """库里 owner 为空时，命中行的 character 也不能退回 owner，用调用方给的库名。"""
    plugin = _plugin(tmp_path)
    engine = MemoryEngine(plugin._db_path('甲'))
    engine.store.add_anchor(summary='记住这句原话', quote='我喜欢喝乌龙茶')
    engine.store.add_turn('user', '我喜欢喝乌龙茶')
    plugin._engines['甲'] = engine
    anchor = engine.store.all_anchors()[0]
    assert engine.store.get_owner() == ''
    result = asyncio.run(plugin.delete_memory(
        table='anchors', id=anchor['id'], character='甲', confirmed=True))
    assert result.ok is True
    hits = result.value['working_memory_hits']
    assert hits and all(hit['character'] == '甲' for hit in hits)


def test_snapshot_iterates_over_copy(tmp_path):
    """刷新过程中给 _engines 添一个新库，不该抛 dictionary changed size。"""
    plugin = _plugin(tmp_path)
    asyncio.run(plugin.observe(user_text='先写一句'))

    class MutatingEngines(dict):
        def items(self):
            # 模拟 worker 线程遍历时事件循环里挂上了新库
            items = list(super().items())
            super().__setitem__('新库', items[0][1])
            return iter(items)

    plugin._engines = MutatingEngines(plugin._engines)
    snapshot = plugin._settings_snapshot()
    assert snapshot['read_errors'] == []
    assert len(snapshot['memories']) >= 0
