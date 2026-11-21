"""插件层烟测：假 SDK 下能导入、只读召回、写入带证据、面板动作不炸。"""

from __future__ import annotations

import asyncio

from temporal_memory import TemporalMemoryPlugin


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


def test_package_imports_with_fake_sdk():
    assert issubclass(TemporalMemoryPlugin, object)
    for name in ('observe', 'remember', 'recall', 'timeline', 'recall_tool',
                 'confirm_memory', 'pin_memory', 'correct_memory', 'archive_memory',
                 'resolve_conflict', 'resolve_orphan', 'undo_last',
                 'export_memories', 'purge_memories'):
        assert hasattr(TemporalMemoryPlugin, name), name


def test_no_push_message_surface():
    # 原版注入方式必须彻底消失，而不是留一个开关
    assert not hasattr(TemporalMemoryPlugin, '_push_read')
    source = (TemporalMemoryPlugin.__module__ and __import__(
        'temporal_memory', fromlist=['__file__']).__file__)
    assert source
    text = open(source, encoding='utf-8').read()
    assert 'self.push_message' not in text
    assert 'ai_behavior' not in text


def test_settings_payload_covers_defaults(tmp_path):
    plugin = _make_plugin(tmp_path)
    result = asyncio.run(plugin.update_settings(
        {'enabled': False, 'dormancy_event_days': '30'}))
    assert getattr(result, 'ok', False) is True
    assert plugin._settings['enabled'] is False
    assert plugin._settings['dormancy_event_days'] == 30
    assert plugin._settings['dormancy_preference_days'] == 180
    assert 'inject_enabled' not in plugin._settings
    asyncio.run(plugin.update_settings({'enabled': 'false'}))
    assert plugin._settings['enabled'] is False


def test_settings_payload_exposes_capacity(tmp_path):
    """面板的容量分母来自后端常量，不能在 TSX 里另写一份。"""
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我喜欢喝美式'))
    context = asyncio.run(plugin.settings_context())
    assert context['capacity'] == {'per_role': 5000, 'max_engines': 32}
    assert context['status']['stats']['max_role_used'] == 2


def test_observe_rejects_empty_text(tmp_path):
    plugin = _make_plugin(tmp_path)
    result = asyncio.run(plugin.observe(user_text='   '))
    assert getattr(result, 'ok', True) is False


def test_observe_rejects_prefixed_source_event_id(tmp_path):
    """模型不能自带 chat_turn: / manual: 前缀来抢先占位宿主事件。"""
    plugin = _make_plugin(tmp_path)
    result = asyncio.run(plugin.observe(
        user_text='今天出门忘带伞', source_event_id='chat_turn:evt-victim'))
    assert getattr(result, 'ok', True) is False
    assert '前缀' in result.error.message
    # 宿主 id 若含其他冒号仍可透传，不做过度拦截。
    accepted = asyncio.run(plugin.observe(
        user_text='今天出门忘带伞', source_event_id='host:evt-1'))
    assert accepted.ok is True


def test_db_path_hash_stops_sanitized_collision(tmp_path):
    plugin = _make_plugin(tmp_path)
    assert plugin._db_path('a b') != plugin._db_path('a_b')


def test_engine_capacity_reports_clear_error(tmp_path, monkeypatch):
    import temporal_memory as tm

    monkeypatch.setattr(tm, 'MAX_ENGINES', 1)
    plugin = _make_plugin(tmp_path)
    assert asyncio.run(plugin._ensure_engine('甲')) is not None
    result = asyncio.run(plugin.recall_tool(
        query='旧事', _ctx={'host_reply': {}, 'lanlan_name': '乙'}))
    assert result['text'] == ''
    assert '上限 1' in result['error']
    assert '清理旧角色库' in result['error']


def test_purge_memories_entry_frees_old_archive(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine(''))
    old = engine.store.add_anchor(summary='旧归档', quote='原话', status='archived')
    with engine.store._session() as conn:
        conn.execute('UPDATE anchors SET updated_at=100 WHERE id=?', (old['id'],))
    result = asyncio.run(plugin.purge_memories())
    assert getattr(result, 'ok', False) is True
    assert result.value['total'] == 1


def test_timer_is_coroutine():
    assert asyncio.iscoroutinefunction(TemporalMemoryPlugin.on_maintenance_timer)


def test_timer_name_is_not_underscore_prefixed():
    assert not 'on_maintenance_timer'.startswith('_')
    assert hasattr(TemporalMemoryPlugin, 'on_maintenance_timer')


def test_character_comes_from_ctx(tmp_path):
    from types import SimpleNamespace

    plugin = _make_plugin(tmp_path)
    plugin.ctx = SimpleNamespace(current_lanlan='猫娘')
    assert plugin._character({}) == '猫娘'
    plugin.ctx = None
    assert plugin._character({'_ctx': {'host_reply': {}, 'lanlan_name': '宿主注入'}}) == '宿主注入'
    # 模型自己填的 _ctx 没有 host_reply，不可信
    assert plugin._character({'_ctx': {'lanlan_name': '伪造角色'}}) == ''
    assert plugin._character(None) == ''


def test_db_path_partitions_by_character(tmp_path):
    plugin = _make_plugin(tmp_path)
    default = plugin._db_path()
    other = plugin._db_path('猫娘/二号')
    assert default.endswith('memory.db')
    assert 'memory-猫娘_二号-' in other and other.endswith('.db')
    assert default != other


def test_observe_end_to_end_writes_evidence_without_push(tmp_path):
    plugin = _make_plugin(tmp_path)
    pushed = []
    plugin.push_message = lambda **kwargs: pushed.append(kwargs) or {'submitted': True}

    first = asyncio.run(plugin.observe(
        user_text='记住，我对猫毛过敏', assistant_text='好的',
        source_event_id='evt_1'))
    assert getattr(first, 'ok', False) is True
    assert first.value['anchored'] is True
    assert 'injection' not in first.value

    engine = plugin._engines['']
    anchor = engine.store.all_anchors()[0]
    assert anchor['quote'] == '记住，我对猫毛过敏'
    assert anchor['source_event_id'] == 'chat_turn:evt_1'
    assert anchor['status'] == 'active'
    assert pushed == []

    second = asyncio.run(plugin.observe(user_text='我家猫又掉毛了，过敏犯了'))
    assert getattr(second, 'ok', False) is True
    assert 'injection' not in second.value
    assert pushed == []


def test_recall_tool_returns_evidence_only(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我对猫毛过敏', source_event_id='evt_2'))
    result = asyncio.run(plugin.recall_tool(query='猫毛'))
    assert result['anchors'] >= 1
    assert '猫毛' in result['text']


def test_settings_context_lists_memories_and_conflicts(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我喜欢喝美式'))
    context = asyncio.run(plugin.settings_context())
    assert context['status']['ready'] is True
    assert any(item['quote'] == '我喜欢喝美式' for item in context['memories'])
    assert context['conflicts'] == []


def test_settings_context_normalizes_conflict_fields(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.observe(user_text='我喜欢猫'))
    asyncio.run(plugin.observe(user_text='我讨厌猫'))
    context = asyncio.run(plugin.settings_context())
    assert context['conflicts']
    conflict = context['conflicts'][0]
    assert conflict['table'] == 'semantics'
    assert {item['text'] for item in context['conflicts']} == {'我喜欢猫', '我讨厌猫'}
    assert all(item['quote'] for item in context['conflicts'])


def test_settings_context_counts_visible_memories_only(tmp_path):
    """截断提示按面板可见的 active/dormant 计，不被 archived/pending 挤占。"""
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我喜欢喝美式'))
    store = plugin._engines[''].store
    for index in range(260):
        store.add_anchor(summary=f'旧归档 {index}', quote='原话', status='archived')
    context = asyncio.run(plugin.settings_context())
    assert len(context['memories']) == 2
    assert context['memories_truncated'] is False
    assert all(item['status'] in {'active', 'dormant'} for item in context['memories'])

    for index in range(250):
        store.add_anchor(summary=f'活跃 {index}', quote='原话')
    context = asyncio.run(plugin.settings_context())
    assert len(context['memories']) == 240
    assert context['memories_truncated'] is True


def test_confirm_and_undo_round_trip(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我喜欢喝美式'))
    engine = plugin._engines['']
    item = engine.store.all_memories('semantics')[0]
    assert item['status'] == 'active'

    result = asyncio.run(plugin.archive_memory(table='semantics', id=item['id']))
    assert getattr(result, 'ok', False) is True
    assert engine.store.get_memory('semantics', item['id'])['status'] == 'archived'

    undo = asyncio.run(plugin.undo_last())
    assert getattr(undo, 'ok', False) is True
    assert engine.store.get_memory('semantics', item['id'])['status'] == 'active'


def test_undo_last_maps_capacity_and_missing_target(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我喜欢喝美式'))
    engine = plugin._engines['']
    monkeypatch.setattr(engine.store, 'last_revision',
                        lambda: {'ts': 1, 'id': 'rev-1'})

    monkeypatch.setattr(engine, 'undo',
                        lambda: {'ok': False, 'reason': 'capacity',
                                 'restored': 0})
    result = asyncio.run(plugin.undo_last())
    assert getattr(result, 'ok', True) is False
    assert '上限' in result.error.message

    monkeypatch.setattr(engine, 'undo',
                        lambda: {'ok': False, 'reason': 'target_missing',
                                 'restored': 0})
    result = asyncio.run(plugin.undo_last())
    assert getattr(result, 'ok', True) is False
    assert '已不存在' in result.error.message


def test_correct_memory_updates_text_but_quote_is_readonly(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我喜欢喝美式'))
    engine = plugin._engines['']
    item = engine.store.all_memories('semantics')[0]
    result = asyncio.run(plugin.correct_memory(
        table='semantics', id=item['id'], text='我喜欢喝燕麦拿铁',
        tags='饮品, 咖啡'))
    assert getattr(result, 'ok', False) is True
    updated = engine.store.get_memory('semantics', item['id'])
    assert updated['content'] == '我喜欢喝燕麦拿铁'
    assert updated['quote'] == '我喜欢喝美式'
    assert updated['tags'] == '咖啡,饮品'
    rejected = engine.correct('semantics', item['id'], quote='改写原话')
    assert rejected['ok'] is False and rejected['reason'] == 'quote_readonly'


def test_export_memories_contains_quote(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我对猫毛过敏'))
    payload = asyncio.run(plugin.export_memories())
    assert payload.value['schema'] == 4
    assert any(item['quote'] == '我对猫毛过敏' for item in payload.value['anchors'])


def test_remember_defaults_unpinned_and_whitelists_tags(tmp_path):
    plugin = _make_plugin(tmp_path)
    result = asyncio.run(plugin.remember(
        text='我喜欢喝美式', tags=['饮品', 'bad tag', '饮品', '超' * 30, '猫']))
    assert getattr(result, 'ok', False) is True
    engine = plugin._engines['']
    anchor = engine.store.all_anchors()[0]
    assert anchor['pinned'] == 0
    assert anchor['tags'] == '猫,饮品'


def test_write_rate_limit_per_character(tmp_path):
    plugin = _make_plugin(tmp_path)
    assert all(plugin._allow_write('猫娘') for _ in range(30))
    assert plugin._allow_write('猫娘') is False
    assert plugin._allow_write('别的角色') is True


def test_remember_clamps_infinite_ts(tmp_path):
    plugin = _make_plugin(tmp_path)
    result = asyncio.run(plugin.remember(
        text='我对猫毛过敏', ts=float('inf')))
    assert getattr(result, 'ok', False) is True
    anchor = plugin._engines[''].store.all_anchors()[0]
    assert abs(anchor['ts'] - int(__import__('time').time())) < 5


def test_recall_query_is_truncated_to_200(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我对猫毛过敏'))
    seen: dict[str, str] = {}
    original = plugin._engines[''].recall

    def capture(query, *args, **kwargs):
        seen['query'] = query
        return original(query, *args, **kwargs)

    plugin._engines[''].recall = capture
    asyncio.run(plugin.recall_tool(query='猫' * 300))
    assert len(seen['query']) == 200


def test_orphan_pending_is_visible_and_resolvable(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    item = engine.store.add_anchor(
        summary='没有冲突组的待确认', quote='原话', status='pending')
    snapshot = plugin._settings_snapshot()
    assert snapshot['orphan_pending'][0]['id'] == item['id']
    assert snapshot['orphan_pending'][0]['text'] == '没有冲突组的待确认'

    result = asyncio.run(plugin.resolve_orphan(
        table='anchors', id=item['id'], keep=True))
    assert result.ok is True
    assert engine.store.get_memory('anchors', item['id'])['status'] == 'active'


def test_orphan_pending_can_be_archived(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    item = engine.store.add_anchor(
        summary='孤立待确认', quote='原话', status='pending')
    result = asyncio.run(plugin.resolve_orphan(
        table='anchors', id=item['id'], keep=False))
    assert result.ok is True
    assert engine.store.get_memory('anchors', item['id'])['status'] == 'archived'


def test_all_memories_empty_status_filter_returns_nothing(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我对猫毛过敏'))
    store = plugin._engines[''].store
    assert store.all_memories('anchors', statuses=()) == []
    assert store.all_memories('anchors', statuses=None)


def test_all_memories_puts_pinned_inside_window(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.remember(text='我对猫毛过敏'))
    store = plugin._engines[''].store
    old_pinned = store.add_anchor(summary='很早的固定条目', ts=100, pinned=True)
    for i in range(300):
        store.add_anchor(summary=f'新条目 {i}', ts=1000 + i)
    rows = store.all_memories('anchors', 300)
    assert rows[0]['id'] == old_pinned['id']
    assert rows[0]['pinned'] == 1
    assert len(rows) == 300
    panel_rows = plugin._engines[''].list_memories(300, statuses=('active', 'dormant'))
    assert panel_rows[0]['id'] == old_pinned['id']
    snapshot = plugin._settings_snapshot()
    assert snapshot['memories'][0]['id'] == old_pinned['id']


def test_orphan_truncation_is_detected_in_one_role(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    for i in range(130):
        engine.store.add_anchor(
            summary=f'孤立待确认 {i}', quote=f'原话 {i}', status='pending')
    snapshot = plugin._settings_snapshot()
    assert len(snapshot['orphan_pending']) == 120
    assert snapshot['orphan_pending_truncated'] is True


def test_resolve_orphan_rejects_non_orphan_targets(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    active = engine.store.add_anchor(summary='正常条目', quote='原话')
    result = asyncio.run(plugin.resolve_orphan(
        table='anchors', id=active['id'], keep=False))
    assert result.ok is False
    assert '未分组' in result.error.message

    conflicted = engine.store.add_anchor(
        summary='冲突组待确认', quote='原话', status='pending',
        conflict_group='group-x')
    result = asyncio.run(plugin.resolve_orphan(
        table='anchors', id=conflicted['id'], keep=True))
    assert result.ok is False
    assert '未分组' in result.error.message


def test_conflict_truncation_keeps_whole_groups(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    for i in range(121):
        for member in range(2):
            engine.store.add_anchor(
                summary=f'冲突 {i}-{member}', quote='原话', status='pending',
                conflict_group=f'group-{i:03d}')
    snapshot = plugin._settings_snapshot()
    rows = snapshot['conflicts']
    assert snapshot['conflicts_truncated'] is True
    assert len(rows) == 240
    assert len({item['conflict_group'] for item in rows}) == 120


def test_dormant_does_not_evict_older_active_in_window(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    for i in range(60):
        engine.store.add_anchor(summary=f'旧 active {i}', ts=1000 + i)
    for i in range(250):
        engine.store.add_anchor(
            summary=f'新 dormant {i}', ts=2000 + i, status='dormant')
    rows = engine.store.all_memories('anchors', 300)
    assert sum(1 for row in rows if row['status'] == 'active') == 60


def test_same_conflict_group_name_in_two_roles_is_separate(tmp_path):
    plugin = _make_plugin(tmp_path)
    first = asyncio.run(plugin._ensure_engine('猫娘'))
    second = asyncio.run(plugin._ensure_engine('别的角色'))
    left = first.store.add_anchor(
        summary='甲库说法', quote='原话', status='pending',
        conflict_group='same-name')
    right = second.store.add_anchor(
        summary='乙库说法', quote='原话', status='pending',
        conflict_group='same-name')
    snapshot = plugin._settings_snapshot()
    assert {item['character'] for item in snapshot['conflicts']} == {'猫娘', '别的角色'}
    assert len(snapshot['conflicts']) == 2

    result = asyncio.run(plugin.resolve_conflict(
        group='same-name', keep_id=left['id'], character='猫娘'))
    assert result.ok is True
    assert first.store.get_memory('anchors', left['id'])['status'] == 'active'
    assert second.store.get_memory('anchors', right['id'])['status'] == 'pending'


def test_orphan_pending_sorted_by_time_across_roles(tmp_path):
    plugin = _make_plugin(tmp_path)
    first = asyncio.run(plugin._ensure_engine('猫娘'))
    second = asyncio.run(plugin._ensure_engine('别的角色'))
    first.store.add_anchor(summary='较晚', quote='原话', status='pending', ts=2000)
    second.store.add_anchor(summary='较早', quote='原话', status='pending', ts=1000)
    snapshot = plugin._settings_snapshot()
    assert [item['text'] for item in snapshot['orphan_pending'][:2]] == ['较早', '较晚']


def test_all_memories_none_limit_is_unbounded(tmp_path):
    plugin = _make_plugin(tmp_path)
    store = asyncio.run(plugin._ensure_engine('')).store
    for i in range(401):
        store.add_anchor(summary=f'条目 {i}', ts=1000 + i)
    assert len(store.all_memories('anchors', None)) == 401
    assert len(store.all_memories('anchors', 400)) == 400


def test_engine_export_caps_rows_but_reports_truncation(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    for i in range(3):
        engine.store.add_anchor(summary=f'条目 {i}', ts=1000 + i)
    dumped = engine.export(max_rows=2)
    assert len(dumped['anchors']) == 2
    assert dumped['truncated']['anchors'] is True
    assert dumped['truncated']['semantics'] is False


def test_plugin_export_surfaces_truncation_warning(tmp_path, monkeypatch):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    monkeypatch.setattr(engine, 'export', lambda: {
        'exported_at': 1, 'schema': 4, 'anchors': [{'id': 'epi_x'}],
        'semantics': [], 'truncated': {'anchors': True, 'semantics': False},
    })
    payload = asyncio.run(plugin.export_memories()).value
    assert payload['truncated']['anchors'] is True
    assert '超过上限' in payload['warning']


def test_resolve_conflict_reports_keep_id_not_in_group(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    engine.store.add_anchor(
        summary='甲', quote='原话', status='pending', conflict_group='g-1')
    result = asyncio.run(plugin.resolve_conflict(
        group='g-1', keep_id='missing-id', character='猫娘'))
    assert result.ok is False
    assert '不在这组' in result.error.message


def test_resolve_conflict_empty_character_targets_default_db(tmp_path):
    plugin = _make_plugin(tmp_path)
    first = asyncio.run(plugin._ensure_engine('猫娘'))
    other = asyncio.run(plugin._ensure_engine('别的角色'))
    plugin._engines[''] = first
    left = first.store.add_anchor(
        summary='默认库', quote='原话', status='pending', conflict_group='g-2')
    right = other.store.add_anchor(
        summary='别的库', quote='原话', status='pending', conflict_group='g-2')
    result = asyncio.run(plugin.resolve_conflict(
        group='g-2', keep_id=left['id'], character=''))
    assert result.ok is True
    assert other.store.get_memory('anchors', right['id'])['status'] == 'pending'


def test_working_memory_is_visible_and_exported(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.observe(user_text='今天出门忘带伞', assistant_text='记得带伞'))
    snapshot = plugin._settings_snapshot()
    assert [turn['role'] for turn in snapshot['working_memory']] == ['用户', '助手']
    payload = asyncio.run(plugin.export_memories()).value
    assert len(payload['working_memory']) == 2
    assert payload['working_memory'][0]['content'] == '今天出门忘带伞'


def test_delete_memory_permanent_requires_confirmation(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    item = engine.store.add_anchor(summary='待删除', quote='原话')
    rejected = asyncio.run(plugin.delete_memory(
        table='anchors', id=item['id'], confirmed=False))
    assert rejected.ok is False
    accepted = asyncio.run(plugin.delete_memory(
        table='anchors', id=item['id'], confirmed=True))
    assert accepted.ok is True
    assert engine.store.get_memory('anchors', item['id']) is None


def test_clear_all_wipes_memories_and_working_memory(tmp_path):
    plugin = _make_plugin(tmp_path)
    asyncio.run(plugin.observe(user_text='今天出门忘带伞', assistant_text='记得带伞'))
    asyncio.run(plugin.remember(text='我对猫毛过敏'))
    result = asyncio.run(plugin.clear_memories(confirmed=True))
    assert result.ok is True
    engine = plugin._engines['']
    stats = engine.store.stats()
    assert stats['anchors'] == 0
    assert stats['semantics'] == 0
    assert stats['working_turns'] == 0
    assert engine.store.recent_turns(8) == []


def test_disabled_switch_blocks_recall_too(tmp_path):
    from temporal_memory.engine import MemoryEngine
    plugin = _make_plugin(tmp_path)
    engine = MemoryEngine(plugin._db_path('猫娘'))
    engine.store.add_anchor(summary='用户喜欢猫', quote='我喜欢猫')
    engine.store.close()
    plugin._settings['enabled'] = False
    result = asyncio.run(plugin.recall_tool(query='猫'))
    assert result['disabled'] is True
    assert result['text'] == ''
    panel = asyncio.run(plugin.recall(query='猫'))
    assert panel.ok is False
    assert '总开关' in panel.error.message


def test_clear_all_includes_unloaded_character_databases(tmp_path):
    from temporal_memory.engine import MemoryEngine
    plugin = _make_plugin(tmp_path)
    path = plugin._db_path('离线角色')
    engine = MemoryEngine(path)
    engine.store.add_anchor(summary='离线', quote='原话')
    engine.store.close()
    result = asyncio.run(plugin.clear_memories(confirmed=True))
    assert result.ok is True
    assert result.value['databases'] == 1
    reopened = MemoryEngine(path)
    assert reopened.store.stats()['anchors'] == 0
    reopened.store.close()


def test_delete_memory_targets_unloaded_character_database(tmp_path):
    from temporal_memory.engine import MemoryEngine
    plugin = _make_plugin(tmp_path)
    path = plugin._db_path('离线角色')
    engine = MemoryEngine(path)
    item = engine.store.add_semantic('离线偏好', quote='我喜欢猫')
    engine.store.close()
    result = asyncio.run(plugin.delete_memory(
        table='semantics', id=item['id'], character='离线角色', confirmed=True))
    assert result.ok is True
    reopened = MemoryEngine(path)
    assert reopened.store.get_memory('semantics', item['id']) is None
    reopened.store.close()


def test_archived_memories_are_visible_for_permanent_delete(tmp_path):
    plugin = _make_plugin(tmp_path)
    engine = asyncio.run(plugin._ensure_engine('猫娘'))
    item = engine.store.add_anchor(summary='已移除', quote='原话')
    engine.archive('anchors', item['id'])
    snapshot = plugin._settings_snapshot()
    assert [row['id'] for row in snapshot['archived']] == [item['id']]
    result = asyncio.run(plugin.delete_memory(
        table='anchors', id=item['id'], character='猫娘', confirmed=True))
    assert result.ok is True
    assert plugin._settings_snapshot()['archived'] == []

