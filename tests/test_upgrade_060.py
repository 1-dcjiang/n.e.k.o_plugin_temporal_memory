"""v0.6.0：置信度标签、假设区、token 预算、离线整理与敏感导出。"""

from __future__ import annotations

import asyncio

from temporal_memory import scoring
from temporal_memory.engine import MemoryEngine


def _engine(tmp_path, settings=None):
    return MemoryEngine(str(tmp_path / 'memory.db'), settings or {})


def test_hypothesis_cues_are_conservative():
    assert scoring.is_hypothesis('假设我是猫，你会怎么养我')
    assert scoring.is_hypothesis('来玩个角色扮演，你当店长')
    assert scoring.is_hypothesis('what if we lived by the sea')
    assert not scoring.is_hypothesis('我对猫毛过敏，家里不能养猫')


def test_hypothesis_is_kept_but_not_recalled(tmp_path):
    engine = _engine(tmp_path)
    result = engine.observe('假设我是一只猫，你会怎么照顾我', ts=1000)
    assert result['hypothesis'] is True
    assert result['anchor']['status'] == 'hypothesis'
    assert result['anchor']['kind'] == 'hypothesis'
    assert engine.recall('一只猫')['anchors'] == []
    assert engine.recall('一只猫')['semantics'] == []
    rows = engine.list_memories(10, statuses=('hypothesis',))
    assert rows and rows[0]['can_recall'] is False


def test_recall_labels_user_quote_and_inference(tmp_path):
    engine = _engine(tmp_path)
    engine.remember('我对猫毛过敏，家里不能养猫', ts=1000)
    result = engine.recall('猫毛 过敏')
    block = result['prompt_block']
    assert '用户原话' in block
    assert '插件推断' in block
    assert '没有记录就说' in block
    assert '推测口吻' in block


def test_token_budget_is_capped(tmp_path):
    engine = _engine(tmp_path, {'recall_char_budget': 1200,
                                'recall_token_budget': 40})
    for index in range(8):
        engine.remember(f'全飞秒术后第 {index} 天要滴眼药水，别揉眼睛', ts=1000 + index)
    result = engine.recall('全飞秒 眼药水')
    assert result['token_budget'] == 40
    assert result['tokens'] <= 40 + 20
    assert result['chars'] <= 1200


def test_consolidate_expires_hypothesis_and_dedupes(tmp_path):
    engine = _engine(tmp_path, {'hypothesis_retention_days': 14})
    engine.observe('假设我是一只猫', ts=1000)
    engine.store.add_semantic('我喜欢喝美式', tags=['咖啡'], kind='preference',
                              status='active', score=0.8)
    engine.store.add_semantic('我喜欢喝美式', tags=['咖啡'], kind='preference',
                              status='active', score=0.2)
    counts = engine.consolidate(now=1000 + 15 * 86400)
    assert counts['hypotheses'] == 1
    assert counts['semantics'] == 1
    active = engine.store.all_memories('semantics', 10, statuses=('active',))
    assert len(active) == 1


def test_sensitive_export_and_clear(tmp_path):
    engine = _engine(tmp_path)
    engine.remember('今天是我生日，第一次一起过纪念日', tags=['里程碑'], ts=1000)
    exported = engine.export()
    assert exported['redacted_sensitive'] == 1
    assert not any(item.get('quote') == '今天是我生日，第一次一起过纪念日'
                   for item in exported['anchors'])
    full = engine.export(include_sensitive=True)
    assert any(item.get('quote') == '今天是我生日，第一次一起过纪念日'
               for item in full['anchors'])
    cleared = engine.clear_sensitive()
    assert cleared['total'] == 1
    assert engine.store.all_memories('anchors', 10) == []


def test_plugin_recall_tool_and_clear_sensitive_entries(tmp_path):
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
    asyncio.run(plugin.remember(text='今天是我生日，纪念日', tags=['里程碑']))
    result = asyncio.run(plugin.export_memories())
    assert result.value['redacted_sensitive'] == 1
    cleared = asyncio.run(plugin.clear_sensitive_memories(confirmed=True))
    assert cleared.ok is True
    assert cleared.value['total'] == 1
