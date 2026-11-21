"""0.5.18：删除路径收掉 FTS / WAL 残留，用唯一词验证。

FTS5 表是普通 contentful 表（`fts5(id UNINDEXED, summary, tags)`），不是
external content / contentless：删除默认只写删除标记，旧段里的词条要等合并才重写。
3.44 起用 `secure-delete` 配置直接抹除，低版本退回删除后 `optimize`；
另用 `wal_checkpoint(TRUNCATE)` 清掉 WAL 里的旧帧。
"""

from __future__ import annotations

import sqlite3

from temporal_memory.memory_store import MemoryStore

TOKEN = 'zq7x9k'


def _blob(*paths) -> bytes:
    out = b''
    for path in paths:
        try:
            out += open(path, 'rb').read()
        except FileNotFoundError:
            continue
    return out


def test_secure_delete_configured_on_real_sqlite(tmp_path):
    store = MemoryStore(str(tmp_path / 'm.db'))
    try:
        assert store.fts_mode != 'none'
        expected = sqlite3.sqlite_version_info >= (3, 44, 0)
        assert store._fts_secure_delete is expected
    finally:
        store.close()


def test_delete_anchor_leaves_no_token_in_db_or_wal(tmp_path):
    db = tmp_path / 'm.db'
    store = MemoryStore(str(db))
    try:
        assert store.fts_mode != 'none'
        anchor = store.add_anchor(summary=f'唯一词 {TOKEN} 只用于残留验证', tags='')
        assert store.delete_anchor(anchor['id']) is True
        blob = _blob(db, tmp_path / 'm.db-wal')
        assert TOKEN.encode() not in blob
    finally:
        store.close()


def test_delete_semantic_and_working_turn_also_clean(tmp_path):
    store = MemoryStore(str(tmp_path / 'm.db'))
    try:
        semantic = store.add_semantic(f'语义唯一词 {TOKEN}')
        store.add_turn('user', f'原话唯一词 {TOKEN}')
        store.delete_semantic(semantic['id'])
        turns = store.recent_turns(8)
        store.delete_working_turn(turns[0]['id'])
        blob = _blob(tmp_path / 'm.db', tmp_path / 'm.db-wal')
        assert TOKEN.encode() not in blob
    finally:
        store.close()


def test_optimize_fallback_removes_token(tmp_path):
    """模拟 3.44 以下：没有 secure-delete，只能删除后 optimize 重写旧段。"""
    db = tmp_path / 'm.db'
    store = MemoryStore(str(db))
    try:
        store._fts_secure_delete = False
        anchor = store.add_anchor(summary=f'回落唯一词 {TOKEN}', tags='')
        store.delete_anchor(anchor['id'])
        assert TOKEN.encode() not in _blob(db, tmp_path / 'm.db-wal')
    finally:
        store.close()


def test_control_plain_fts_delete_keeps_token(tmp_path):
    """对照组：不配 secure-delete、不 optimize 时，旧段里能搜到这个词。"""
    db = tmp_path / 'control.db'
    conn = sqlite3.connect(db)
    try:
        conn.execute('CREATE VIRTUAL TABLE fts USING fts5(body)')
        conn.execute('INSERT INTO fts(body) VALUES(?)', (TOKEN,))
        conn.commit()
        conn.execute('DELETE FROM fts')
        conn.commit()
    finally:
        conn.close()
    assert TOKEN.encode() in _blob(db)
