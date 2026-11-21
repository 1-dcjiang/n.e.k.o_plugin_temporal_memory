"""存储层：锚点链不变量、FTS 回落、语义邻接。"""

from __future__ import annotations

import sqlite3

import pytest
from temporal_memory.memory_store import MemoryStore


@pytest.fixture()
def store(tmp_path):
    return MemoryStore(tmp_path / 'memory.db')


def _add(store, text, ts, **kwargs):
    return store.add_anchor(summary=text, ts=ts, time_label='', **kwargs)


def test_append_keeps_chain_ordered(store):
    ids = [_add(store, f'第{i}件事', 100 + i)['id'] for i in range(3)]
    chain = store.iter_chain()
    assert [a['id'] for a in chain] == ids
    assert chain[0]['prev_id'] is None
    assert chain[-1]['next_id'] is None
    assert store.chain_integrity()['ok'] is True


def test_delete_middle_repairs_neighbors(store):
    ids = [_add(store, f'事件{i}', 100 + i)['id'] for i in range(4)]
    store.delete_anchor(ids[1])
    chain = [a['id'] for a in store.iter_chain()]
    assert chain == [ids[0], ids[2], ids[3]]
    assert store.chain_integrity()['ok'] is True


def test_delete_head_and_tail(store):
    ids = [_add(store, f'事件{i}', 100 + i)['id'] for i in range(3)]
    store.delete_anchor(ids[0])
    assert store.iter_chain()[0]['id'] == ids[1]
    store.delete_anchor(ids[2])
    assert store.iter_chain()[-1]['id'] == ids[1]
    assert store.chain_integrity()['ok'] is True


def test_reverse_iteration_walks_prev(store):
    ids = [_add(store, f'事件{i}', 100 + i)['id'] for i in range(3)]
    assert [a['id'] for a in store.iter_chain(ascending=False)][0] == ids[-1]


def test_search_anchors_and_semantics(store):
    anchor = _add(store, '用户: 办了全飞秒手术，术后要滴眼药水', 100)
    store.add_semantic('全飞秒术后需要按医嘱滴眼药水', tags=['全飞秒'], attach_anchor_id=anchor['id'])

    hits = store.search_anchors('全飞秒', 3)
    assert hits and hits[0]['id'] == anchor['id']
    sem = store.search_semantics('全飞秒', 3)
    assert sem and '眼药水' in sem[0]['content']


def test_semantics_expand_two_hops(store):
    a = store.add_semantic('苏大邮箱要用域名邮箱配置', tags=['邮箱', '苏大'])
    b = store.add_semantic('苏大VPN是域名邮箱的前置', tags=['邮箱', '苏大'])
    store.add_semantic('VPN 需要客户端', tags=['VPN'])
    if b:
        store.add_semantic('VPN 客户端在信息门户下载', tags=['VPN', '客户端'])
    neighbours = store.expand_semantics([a['id']], hops=2, limit=4)
    assert any(item['id'] == b['id'] for item in neighbours)


def test_each_tag_gets_neighbor_quota(store):
    """高频标签不能把 50 个名额占满，让后面的低频标签拿不到邻居。"""
    for index in range(55):
        store.add_semantic(f'高频标签条目 {index}', tags=['高频'])
    rare = store.add_semantic('低频标签条目', tags=['低频'])
    item = store.add_semantic('同时命中两个标签', tags=['高频', '低频'])
    with store._session() as conn:
        dsts = {row['dst'] for row in conn.execute(
            'SELECT dst FROM topics_edges WHERE src=?', (item['id'],)).fetchall()}
    assert rare['id'] in dsts


def test_third_tag_still_gets_neighbor_quota(store):
    """三个标签时也要均分总量，不能让前两个各 50 把第三个饿死。"""
    for index in range(55):
        store.add_semantic(f'高频一 {index}', tags=['高频一'])
        store.add_semantic(f'高频二 {index}', tags=['高频二'])
    rare = store.add_semantic('第三标签条目', tags=['第三标签'])
    item = store.add_semantic('同时命中三个标签', tags=['高频一', '高频二', '第三标签'])
    with store._session() as conn:
        dsts = {row['dst'] for row in conn.execute(
            'SELECT dst FROM topics_edges WHERE src=?', (item['id'],)).fetchall()}
    assert rare['id'] in dsts


def test_working_window_trims_oldest(store):
    for i in range(12):
        store.add_turn('用户', f'第{i}句', ts=100 + i, keep=8)
    turns = store.recent_turns(12)
    assert len(turns) == 8
    assert turns[-1]['content'] == '第11句'


def test_old_fts_rowid_table_is_migrated(tmp_path):
    db = tmp_path / 'memory.db'
    store = MemoryStore(db)
    store.add_anchor(summary='服务器报错日志在 D 盘', ts=100, tags=['报错'])
    with sqlite3.connect(db) as conn:
        conn.execute('DROP TABLE anchors_fts')
        conn.execute('CREATE VIRTUAL TABLE anchors_fts USING fts5(summary, tags)')
        conn.execute('INSERT INTO anchors_fts(rowid, summary, tags) '
                     'SELECT rowid, summary, tags FROM anchors')
    reopened = MemoryStore(db)
    assert reopened.fts_mode != 'none'
    assert reopened.search_anchors('报错', 6)
