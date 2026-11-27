"""SQLite 存储层：时间线锚点链、语义网络、工作记忆窗口。

只依赖标准库 sqlite3，不依赖宿主 SDK，便于单测与后续替换后端。
所有方法都是同步的，插件层用 asyncio.to_thread 包一层，避免阻塞事件循环。
"""

from __future__ import annotations

import json
import logging
import re
import sqlite3
import threading
import time
import uuid
from collections.abc import Iterable
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from .scoring import DOMAIN_TERMS, quote_matches

_LOG = logging.getLogger(__name__)

SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS meta (
    k TEXT PRIMARY KEY,
    v TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS processed_events (
    event_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (event_id, source_kind)
);

CREATE TABLE IF NOT EXISTS event_semantics (
    event_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    semantic_id TEXT NOT NULL,
    created_at INTEGER NOT NULL,
    PRIMARY KEY (event_id, source_kind, semantic_id)
);

CREATE INDEX IF NOT EXISTS idx_event_semantics_event
    ON event_semantics(event_id, source_kind);

CREATE TABLE IF NOT EXISTS anchors (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    time_label TEXT NOT NULL DEFAULT '',
    summary TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'chat_turn',
    kind TEXT NOT NULL DEFAULT 'event',
    status TEXT NOT NULL DEFAULT 'active',
    conflict_group TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0.0,
    confirm_count INTEGER NOT NULL DEFAULT 0,
    last_confirmed_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    emotion REAL NOT NULL DEFAULT 0.0,
    salience TEXT NOT NULL DEFAULT 'warm',
    prev_id TEXT,
    next_id TEXT,
    tags TEXT NOT NULL DEFAULT '',
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_anchors_ts ON anchors(ts);
CREATE INDEX IF NOT EXISTS idx_anchors_sal_ts ON anchors(salience, ts);

CREATE TABLE IF NOT EXISTS semantics (
    id TEXT PRIMARY KEY,
    content TEXT NOT NULL,
    quote TEXT NOT NULL DEFAULT '',
    source_event_id TEXT NOT NULL DEFAULT '',
    source_kind TEXT NOT NULL DEFAULT 'chat_turn',
    kind TEXT NOT NULL DEFAULT 'fact',
    status TEXT NOT NULL DEFAULT 'active',
    conflict_group TEXT NOT NULL DEFAULT '',
    score REAL NOT NULL DEFAULT 0.0,
    confirm_count INTEGER NOT NULL DEFAULT 0,
    last_confirmed_at INTEGER NOT NULL DEFAULT 0,
    updated_at INTEGER NOT NULL DEFAULT 0,
    tags TEXT NOT NULL DEFAULT '',
    attach_anchor_id TEXT,
    recall_count INTEGER NOT NULL DEFAULT 0,
    last_recalled INTEGER NOT NULL DEFAULT 0,
    pinned INTEGER NOT NULL DEFAULT 0,
    created_at INTEGER NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_semantics_anchor ON semantics(attach_anchor_id);

CREATE TABLE IF NOT EXISTS topics_edges (
    src TEXT NOT NULL,
    dst TEXT NOT NULL,
    weight REAL NOT NULL DEFAULT 1.0,
    PRIMARY KEY (src, dst)
);

CREATE TABLE IF NOT EXISTS wm (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    role TEXT NOT NULL,
    content TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS revisions (
    id TEXT PRIMARY KEY,
    ts INTEGER NOT NULL,
    action TEXT NOT NULL,
    before_json TEXT NOT NULL DEFAULT '[]',
    after_json TEXT NOT NULL DEFAULT '[]',
    undone INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_revisions_ts ON revisions(ts);
"""

ANCHOR_SEARCH_MIN_CHARS = 2
MEMORY_KINDS = ('event', 'preference', 'decision', 'fact', 'hypothesis')
MEMORY_STATUSES = ('active', 'dormant', 'pending', 'archived', 'hypothesis')
MAX_TOPIC_NEIGHBORS = 50
MAX_TOPIC_TOTAL = 200
MAX_TOTAL_MEMORIES = 5000
PURGE_ARCHIVED_DAYS = 30
PURGE_DORMANT_DAYS = 365
MAX_REVISIONS = 1000
SCHEMA_VERSION = 4


class MemoryCapacityError(RuntimeError):
    """单库记忆总量达到上限，调用方应整笔回滚并提示用户清理。"""

# 事件查询与 EXPLAIN 测试共用同一份 SQL，避免测试手抄语句和真实代码漂移。
# 部分唯一索引带 WHERE source_event_id <> ''，查询必须显式带同一条件才能命中。
_ANCHOR_EVENT_SQL = (
    "SELECT * FROM anchors WHERE source_event_id=? AND source_event_id<>''")
_ANCHOR_EVENT_KIND_SQL = _ANCHOR_EVENT_SQL + ' AND source_kind=?'
_ANCHOR_EVENT_KIND_LOAD_SQL = (
    _ANCHOR_EVENT_KIND_SQL + ' ORDER BY created_at ASC LIMIT 1')
_ANCHOR_EVENT_FIND_SQL = (
    _ANCHOR_EVENT_SQL + ' ORDER BY created_at DESC LIMIT 1')
_ANCHOR_EVENT_FIND_KIND_SQL = (
    _ANCHOR_EVENT_KIND_SQL + ' ORDER BY created_at DESC LIMIT 1')
_SEMANTIC_EVENT_SQL = (
    "SELECT id FROM semantics WHERE source_event_id=? AND source_event_id<>''")
_SEMANTIC_EVENT_CONTENT_SQL = (
    "SELECT * FROM semantics WHERE source_event_id=? AND source_event_id<>'' "
    'AND source_kind=? AND content=? ORDER BY created_at ASC LIMIT 1')
_PRUNE_PROCESSED_SQL = (
    'DELETE FROM processed_events WHERE created_at < ? '
    'AND NOT EXISTS (SELECT 1 FROM anchors a WHERE '
    'a.source_event_id=processed_events.event_id '
    "AND a.source_event_id<>'' "
    'AND a.source_kind=processed_events.source_kind) '
    'AND NOT EXISTS (SELECT 1 FROM semantics s WHERE '
    's.source_event_id=processed_events.event_id '
    "AND s.source_event_id<>'' "
    'AND s.source_kind=processed_events.source_kind)')


def _semantics_for_event_sql(has_kind: bool) -> str:
    kind_clause = ' AND source_kind=?' if has_kind else ''
    return (
        'SELECT s.* FROM semantics s JOIN ('
        'SELECT semantic_id FROM event_semantics WHERE event_id=?'
        + kind_clause +
        ' UNION ' + _SEMANTIC_EVENT_SQL + kind_clause +
        ') ids ON s.id=ids.semantic_id ORDER BY s.created_at ASC'
    )


def _now() -> int:
    return int(time.time())


def _split_tags(raw: str) -> list[str]:
    return [t for t in (raw or '').split(',') if t]


_TOKEN_SPLIT = re.compile(r'[\s，,。.、；;：:！!？?（）()【】\[\]"“”]+')
_ASCII_WORD = re.compile(r'[A-Za-z][A-Za-z0-9_+-]{1,}')
_CJK_RUN = re.compile(r'[\u4e00-\u9fff]+')
_QUERY_STOP_GRAMS = frozenset({'什么', '怎么', '那个', '这个', '为什', '我们', '你们', '他们', '一下'})


def _query_tokens(query: str, limit: int = 24) -> list[str]:
    """中文查询不能整句当一个 token：领域词优先，再补汉字二元组。

    trigram 只认 ≥3 字，二元组交给 LIKE 兜底；两者都做，避免「上次那个报错
    是什么」这种口语问法零命中。
    """
    query = (query or '').strip()
    if not query:
        return []
    ordered: list[str] = []
    seen: set[str] = set()

    def add(token: str) -> None:
        token = token.strip()
        if not token or token in seen or len(ordered) >= limit:
            return
        seen.add(token)
        ordered.append(token)

    lowered = query.lower()
    for term in DOMAIN_TERMS:
        if term in lowered:
            add(term)
    for match in _ASCII_WORD.findall(lowered):
        add(match)
    for run in _CJK_RUN.findall(query):
        if len(run) <= 6:
            add(run)
            continue
        for i in range(len(run) - 1):
            gram = run[i:i + 2]
            if gram not in _QUERY_STOP_GRAMS:
                add(gram)
    return ordered


class MemoryStore:
    """一个 store 实例对应一个 sqlite 文件；线程安全由内部锁保证。"""

    def __init__(self, db_path: str | Path) -> None:
        self.db_path = str(db_path)
        self._lock = threading.RLock()
        Path(self.db_path).parent.mkdir(parents=True, exist_ok=True)
        self._fts_mode = 'none'
        self._fts_secure_delete = False
        self._wal_mode_set = False
        self._tx_conn: sqlite3.Connection | None = None
        self._unique_index_skipped: list[str] = []
        self._unique_index_retry_at = 0.0
        self.initialize()
        if self._unique_index_skipped:
            # 启动时已经试过一次；首次面板刷新不必立刻再全表扫一遍。
            self._unique_index_retry_at = (
                time.time() + self.UNIQUE_INDEX_RETRY_COOLDOWN)
    # ---------------------------------------------------------------- 基础

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=10)
        conn.row_factory = sqlite3.Row
        if not self._wal_mode_set:
            conn.execute('PRAGMA journal_mode=WAL')
            self._wal_mode_set = True
        conn.execute('PRAGMA synchronous=NORMAL')
        # 删除时释放的数据库页会被擦拭为零，省掉「单条删除后必须 VACUUM 才安心」的负担；
        # 单条删除还会 optimize FTS 旧段并 checkpoint 截断 WAL。仍不覆盖：其他写入留下的
        # WAL 旧帧、导出/备份副本、`*.migrated` 与磁盘快照。
        # 代价是释放页要多写一遍，用户量级（每库几千行）可以接受。
        conn.execute('PRAGMA secure_delete=ON')
        return conn

    @contextmanager
    def _session(self):
        """默认一条语句一个连接；进入 transaction() 后复用同一连接。"""
        if self._tx_conn is not None:
            yield self._tx_conn
            return
        conn = self._connect()
        try:
            yield conn
            self._commit(conn)
        finally:
            conn.close()

    def _commit(self, conn: sqlite3.Connection) -> None:
        """外层事务里不提交，由 transaction() 统一提交或回滚。"""
        if self._tx_conn is None:
            conn.commit()

    _UNIQUE_INDEX_DDL = (
        ('anchors_event', "CREATE UNIQUE INDEX IF NOT EXISTS uniq_anchors_event "
         "ON anchors(source_event_id, source_kind) WHERE source_event_id <> ''",
         'CREATE INDEX IF NOT EXISTS idx_anchors_source_event '
         'ON anchors(source_event_id, source_kind)',
         'DROP INDEX IF EXISTS idx_anchors_source_event'),
        ('semantics_event_content', "CREATE UNIQUE INDEX IF NOT EXISTS "
         "uniq_semantics_event_content "
         "ON semantics(source_event_id, source_kind, content) "
         "WHERE source_event_id <> ''",
         'CREATE INDEX IF NOT EXISTS idx_semantics_source_event '
         'ON semantics(source_event_id, source_kind)',
         'DROP INDEX IF EXISTS idx_semantics_source_event'),
    )
    UNIQUE_INDEX_RETRY_COOLDOWN = 300

    def _ensure_unique_indexes(self, conn: sqlite3.Connection) -> list[str]:
        """建事件级唯一索引；旧库有重复行时返回失败的索引名，不删用户数据。

        两条索引的 DDL 用 savepoint 包住：第二条遇锁失败时，第一条已建/已删的
        结果一起回滚，避免留下半套索引和过期的 warning 状态。
        """
        skipped: list[str] = []
        conn.execute('SAVEPOINT ensure_unique_indexes')
        try:
            for name, ddl, fallback_ddl, fallback_drop in self._UNIQUE_INDEX_DDL:
                try:
                    conn.execute(ddl)
                except sqlite3.IntegrityError:
                    # 唯一索引建不起来（旧库有重复行）时才补非唯一索引供查询使用；
                    # 健康库不白建，查询靠部分唯一索引。
                    skipped.append(name)
                    conn.execute(fallback_ddl)
                else:
                    # 查询都带 source_event_id<>''，能命中部分唯一索引；
                    # 唯一索引已恢复时清掉旧版本遗留的回退索引，避免写放大。
                    conn.execute(fallback_drop)
        except BaseException:
            try:
                conn.execute('ROLLBACK TO ensure_unique_indexes')
                conn.execute('RELEASE ensure_unique_indexes')
            except sqlite3.Error:
                pass
            raise
        conn.execute('RELEASE ensure_unique_indexes')
        return skipped

    def retry_unique_indexes(self, *, force: bool = False) -> list[str]:
        """重复行被用户清理后，不必重启即可重试建唯一索引。

        面板刷新会频繁调用 stats()；加冷却避免每次刷新都全表扫描抢写锁。
        数据库被别的进程锁住时吞掉 OperationalError，状态卡继续显示旧 warning。
        """
        if not self._unique_index_skipped:
            return []
        now = time.time()
        if not force and now < self._unique_index_retry_at:
            return list(self._unique_index_skipped)
        try:
            with self._lock, self._session() as conn:
                self._unique_index_skipped = self._ensure_unique_indexes(conn)
                self._commit(conn)
        except sqlite3.OperationalError:
            self._unique_index_retry_at = now + self.UNIQUE_INDEX_RETRY_COOLDOWN
            return list(self._unique_index_skipped)
        self._unique_index_retry_at = (
            now + self.UNIQUE_INDEX_RETRY_COOLDOWN
            if self._unique_index_skipped else 0.0)
        return list(self._unique_index_skipped)

    @contextmanager
    def transaction(self):
        """跨多个 store 方法的事务：占位、wm、锚点、语义要么全成要么全不成。

        SQLite 的 `BEGIN IMMEDIATE` 先拿写锁，另一个线程/进程只能等，
        因此 claim_event 与后续写入之间不会出现「占位已提交、内容没写」的窗口。
        """
        with self._lock:
            if self._tx_conn is not None:
                yield self._tx_conn
                return
            conn = self._connect()
            self._tx_conn = conn
            try:
                conn.execute('BEGIN IMMEDIATE')
                yield conn
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
            finally:
                self._tx_conn = None
                conn.close()

    def initialize(self) -> None:
        with self._lock, self._session() as conn:
            conn.executescript(SCHEMA_SQL)
            self._migrate(conn)
            self._ensure_fts(conn)
            conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('head', '')")
            conn.execute("INSERT OR IGNORE INTO meta(k, v) VALUES('tail', '')")
            self._commit(conn)

    def _migrate(self, conn: sqlite3.Connection) -> None:
        """零版本迁移：老库补列，并记录 user_version 供以后升级。"""
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(anchors)')}
        if 'pinned' not in columns:
            conn.execute('ALTER TABLE anchors ADD COLUMN pinned INTEGER NOT NULL DEFAULT 0')
            # 老库没有「手动保留」概念，一律按普通条目处理，交给 90/180 天休眠。
        for column, ddl in (
            ('quote', "TEXT NOT NULL DEFAULT ''"),
            ('source_event_id', "TEXT NOT NULL DEFAULT ''"),
            ('source_kind', "TEXT NOT NULL DEFAULT 'chat_turn'"),
            ('kind', "TEXT NOT NULL DEFAULT 'event'"),
            ('status', "TEXT NOT NULL DEFAULT 'active'"),
            ('conflict_group', "TEXT NOT NULL DEFAULT ''"),
            ('score', 'REAL NOT NULL DEFAULT 0.0'),
            ('confirm_count', 'INTEGER NOT NULL DEFAULT 0'),
            ('last_confirmed_at', 'INTEGER NOT NULL DEFAULT 0'),
            ('updated_at', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if column not in columns:
                conn.execute(f'ALTER TABLE anchors ADD COLUMN {column} {ddl}')
        conn.execute("UPDATE anchors SET quote=summary WHERE quote=''")
        conn.execute("UPDATE anchors SET last_confirmed_at=created_at WHERE last_confirmed_at=0")
        conn.execute("UPDATE anchors SET updated_at=created_at WHERE updated_at=0")
        conn.execute('UPDATE anchors SET score=emotion WHERE score=0')

        semantic_columns = {row['name'] for row in conn.execute('PRAGMA table_info(semantics)')}
        for column, ddl in (
            ('quote', "TEXT NOT NULL DEFAULT ''"),
            ('source_event_id', "TEXT NOT NULL DEFAULT ''"),
            ('source_kind', "TEXT NOT NULL DEFAULT 'chat_turn'"),
            ('kind', "TEXT NOT NULL DEFAULT 'fact'"),
            ('status', "TEXT NOT NULL DEFAULT 'active'"),
            ('conflict_group', "TEXT NOT NULL DEFAULT ''"),
            ('score', 'REAL NOT NULL DEFAULT 0.0'),
            ('confirm_count', 'INTEGER NOT NULL DEFAULT 0'),
            ('last_confirmed_at', 'INTEGER NOT NULL DEFAULT 0'),
            ('updated_at', 'INTEGER NOT NULL DEFAULT 0'),
            ('recall_count', 'INTEGER NOT NULL DEFAULT 0'),
            ('last_recalled', 'INTEGER NOT NULL DEFAULT 0'),
            ('pinned', 'INTEGER NOT NULL DEFAULT 0'),
        ):
            if column not in semantic_columns:
                conn.execute(f'ALTER TABLE semantics ADD COLUMN {column} {ddl}')
        conn.execute("UPDATE semantics SET quote=content WHERE quote=''")
        conn.execute("UPDATE semantics SET last_confirmed_at=created_at WHERE last_confirmed_at=0")
        conn.execute("UPDATE semantics SET updated_at=created_at WHERE updated_at=0")

        version = int(conn.execute('PRAGMA user_version').fetchone()[0] or 0)
        if version < 4:
            # 事件 id 加来源前缀；旧库一次性迁移，避免 manual/chat_turn 互相抢占。
            for table in ('anchors', 'semantics'):
                conn.execute(
                    f"UPDATE OR IGNORE {table} SET source_event_id = source_kind || ':' || "
                    "source_event_id WHERE source_event_id <> '' AND "
                    "instr(source_event_id, source_kind || ':') <> 1")
            conn.execute(
                "UPDATE OR IGNORE processed_events "
                "SET event_id = source_kind || ':' || event_id "
                "WHERE event_id <> '' AND "
                "instr(event_id, source_kind || ':') <> 1")
            conn.execute(
                "UPDATE OR IGNORE event_semantics "
                "SET event_id = source_kind || ':' || event_id "
                "WHERE event_id <> '' AND "
                "instr(event_id, source_kind || ':') <> 1")
            conn.execute("INSERT OR REPLACE INTO meta(k, v) "
                         f"VALUES('schema_version', '{SCHEMA_VERSION}')")

        # 老库补完列再建索引：CREATE INDEX 引用不存在的列会直接报错
        conn.execute('CREATE INDEX IF NOT EXISTS idx_anchors_status_ts ON anchors(status, ts)')
        conn.execute('CREATE INDEX IF NOT EXISTS idx_semantics_status ON semantics(status)')
        # 事件级唯一约束：即使绕过 engine 直接调 store，也不能给同一事件建两条锚点。
        # 老库若已有重复行，建索引会失败；保留数据、跳过索引，由上层去重逻辑兜底。
        # 非唯一回退索引只在唯一索引失败时创建，避免启动时白建白删。
        self._unique_index_skipped = self._ensure_unique_indexes(conn)

        conn.execute(f'PRAGMA user_version={SCHEMA_VERSION}')

    def _ensure_fts(self, conn: sqlite3.Connection) -> None:
        existing = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='anchors_fts'"
        ).fetchone()
        columns = {row['name'] for row in conn.execute('PRAGMA table_info(anchors_fts)')} \
            if existing else set()
        # 旧表用隐式 rowid 关联；VACUUM 后可能错位，升级成显式 id 列并重建。
        rebuild = bool(existing) and 'id' not in columns
        if rebuild:
            conn.execute('DROP TABLE anchors_fts')
        for tokenizer in ('trigram', 'unicode61'):
            try:
                conn.execute(
                    'CREATE VIRTUAL TABLE IF NOT EXISTS anchors_fts '
                    f"USING fts5(id UNINDEXED, summary, tags, tokenize='{tokenizer}')"
                )
                self._fts_mode = tokenizer
                self._configure_fts_secure_delete(conn)
                if rebuild:
                    self._rebuild_fts(conn)
                return
            except sqlite3.OperationalError:
                continue
        self._fts_mode = 'none'
        self._fts_secure_delete = False

    def _configure_fts_secure_delete(self, conn: sqlite3.Connection) -> None:
        """让 FTS5 删除时直接抹掉旧段里的词条，而不是只写删除标记。

        3.44 起 FTS5 支持 `secure-delete` 配置项；更低版本只能靠删除后
        `optimize` 合并段来重写索引（见 `_purge_delete_residue`）。
        """
        self._fts_secure_delete = False
        if self._fts_mode == 'none':
            return
        if sqlite3.sqlite_version_info < (3, 44, 0):
            return
        try:
            row = conn.execute(
                "SELECT v FROM anchors_fts_config WHERE k='secure-delete'").fetchone()
            if row is None or str(row['v']) != '1':
                conn.execute(
                    "INSERT INTO anchors_fts(anchors_fts, rank) "
                    "VALUES('secure-delete', 1)")
            self._fts_secure_delete = True
        except sqlite3.Error as exc:
            _LOG.warning('FTS secure-delete 配置失败，将在删除后 optimize: %s', exc)

    def _purge_delete_residue(self) -> None:
        """删除后清掉旧数据：低版本 FTS 合并段 + 截断 WAL 旧帧。

        页面清零由连接上的 `PRAGMA secure_delete=ON` 负责；这里补两件它管不到的：
        FTS 旧段（3.44 以下只能靠 optimize 重写）和 WAL 里尚未截断的旧页。
        失败只记日志：删除本身已经完成，不能因为收尾失败回滚。
        """
        with self._lock:
            try:
                conn = self._connect()
            except sqlite3.Error as exc:
                _LOG.warning('删除后收尾重连失败: %s', exc)
                return
            try:
                # 收尾连接不能跟着 timeout=10 等读事务：忙就立刻放弃并记日志，
                # 不能让一次删除把面板卡住十秒。
                conn.execute('PRAGMA busy_timeout=200')
                if self._fts_mode != 'none' and not self._fts_secure_delete:
                    try:
                        conn.execute(
                            "INSERT INTO anchors_fts(anchors_fts) VALUES('optimize')")
                        conn.commit()
                    except sqlite3.Error as exc:
                        _LOG.warning('删除后 FTS optimize 失败: %s', exc)
                try:
                    busy, _log_frames, _done = conn.execute(
                        'PRAGMA wal_checkpoint(TRUNCATE)').fetchone()
                    if busy:
                        _LOG.warning('删除后 wal checkpoint 被读事务占用，旧帧暂留')
                except sqlite3.Error as exc:
                    _LOG.warning('删除后 wal checkpoint 失败: %s', exc)
            finally:
                conn.close()

    def _rebuild_fts(self, conn: sqlite3.Connection) -> None:
        if self._fts_mode == 'none':
            return
        conn.execute('DELETE FROM anchors_fts')
        rows = conn.execute('SELECT id, summary, tags FROM anchors').fetchall()
        conn.executemany(
            'INSERT INTO anchors_fts(id, summary, tags) VALUES(?,?,?)',
            [(r['id'], r['summary'], r['tags']) for r in rows],
        )

    @property
    def fts_mode(self) -> str:
        return self._fts_mode

    # ---------------------------------------------------------------- 工作记忆

    def add_turn(self, role: str, content: str, ts: int | None = None,
                 keep: int = 8) -> None:
        """写入工作记忆窗口，并把超出窗口的老条目挤出。"""
        ts = _now() if ts is None else int(ts)
        with self._lock, self._session() as conn:
            conn.execute(
                'INSERT INTO wm(ts, role, content) VALUES(?,?,?)',
                (ts, role, content),
            )
            overflow = conn.execute('SELECT COUNT(*) FROM wm').fetchone()[0] - max(0, keep)
            if overflow > 0:
                conn.execute(
                    'DELETE FROM wm WHERE id IN ('
                    'SELECT id FROM wm ORDER BY id ASC LIMIT ?)',
                    (overflow,),
                )
            self._commit(conn)

    def recent_turns(self, limit: int = 8) -> list[dict[str, Any]]:
        with self._lock, self._session() as conn:
            rows = conn.execute(
                'SELECT id, ts, role, content FROM wm ORDER BY id DESC LIMIT ?',
                (max(0, int(limit)),),
            ).fetchall()
        return [dict(r) for r in reversed(rows)]

    def delete_working_turn(self, turn_id: int) -> bool:
        """永久删除工作记忆里的一条原话，不受窗口滚动限制。不可撤销。"""
        try:
            turn_id = int(turn_id)
        except (TypeError, ValueError):
            return False
        with self._lock, self._session() as conn:
            row = conn.execute('SELECT id FROM wm WHERE id=?', (turn_id,)).fetchone()
            if row is None:
                return False
            conn.execute('DELETE FROM wm WHERE id=?', (turn_id,))
            self._commit(conn)
        self._purge_delete_residue()
        return True

    def workmem_hits(self, text: str, limit: int = 8) -> list[dict[str, Any]]:
        """按文本相似匹配工作记忆里含这段原话的行，供删除联动提示。

        相似口径见 `scoring.quote_matches`（去标点互相包含，或二元组相似度 ≥ 0.6），
        不是字面相等：可能命中相近但不是同一条的行，所以面板删前要列预览。
        只做提示不做删除：工作记忆行和记忆条没有关联字段，靠文本比对是唯一
        不建新列的做法，所以宁可少报（短于 4 字不匹配）也不错删。
        """
        needle = (text or '').strip()
        if len(needle) < 4:
            return []
        with self._lock, self._session() as conn:
            rows = conn.execute(
                'SELECT id, ts, role, content FROM wm ORDER BY id DESC').fetchall()
        hits: list[dict[str, Any]] = []
        for row in rows:
            if not quote_matches(needle, row['content']):
                continue
            hits.append({'id': int(row['id']), 'ts': int(row['ts'] or 0),
                         'role': row['role'], 'preview': (row['content'] or '')[:80]})
            if len(hits) >= max(1, int(limit)):
                break
        return hits

    # ---------------------------------------------------------------- 锚点链

    def _meta(self, conn: sqlite3.Connection, key: str) -> str:
        row = conn.execute('SELECT v FROM meta WHERE k=?', (key,)).fetchone()
        return row['v'] if row else ''

    def _set_meta(self, conn: sqlite3.Connection, key: str, value: str) -> None:
        conn.execute('INSERT INTO meta(k, v) VALUES(?, ?) '
                     'ON CONFLICT(k) DO UPDATE SET v=excluded.v', (key, value))

    def _active_total(self, conn: sqlite3.Connection) -> int:
        return int(conn.execute(
            'SELECT (SELECT COUNT(*) FROM anchors '
            "WHERE status IN ('active','pending')) + "
            '(SELECT COUNT(*) FROM semantics '
            "WHERE status IN ('active','pending'))").fetchone()[0])

    def _ensure_capacity(self, conn: sqlite3.Connection, extra: int = 1) -> None:
        # 只限制还会被召回的活跃/待确认条目。面板「移除」改成 archived 或
        # 到期转为 dormant 后应立刻释放名额，否则用户没有自救出口。
        total = self._active_total(conn)
        if total + max(1, int(extra)) > MAX_TOTAL_MEMORIES:
            raise MemoryCapacityError(
                f'活跃/待确认记忆已达上限 {MAX_TOTAL_MEMORIES} 条，请先在面板移除旧条目或清理已休眠记忆')

    def get_owner(self) -> str:
        with self._lock, self._session() as conn:
            return self._meta(conn, 'character')

    def set_owner(self, character: str) -> None:
        with self._lock, self._session() as conn:
            self._set_meta(conn, 'character', character)
            self._commit(conn)

    def add_anchor(self, *, summary: str, ts: int | None = None,
                   time_label: str = '', emotion: float = 0.0,
                   salience: str = 'warm', tags: Iterable[str] = (),
                   pinned: bool = False, quote: str = '',
                   source_event_id: str = '', source_kind: str = 'chat_turn',
                   kind: str = 'event', status: str = 'active',
                   score: float = 0.0, conflict_group: str = '') -> dict[str, Any]:
        """把锚点接到时间线尾部（链表 insert-at-tail）。"""
        with self._lock, self._session() as conn:
            anchor = self._insert_anchor(
                conn, summary=summary, ts=ts, time_label=time_label, emotion=emotion,
                salience=salience, tags=tags, pinned=pinned, quote=quote,
                source_event_id=source_event_id, source_kind=source_kind,
                kind=kind, status=status, score=score, conflict_group=conflict_group)
            self._commit(conn)
        return anchor

    def _insert_anchor(self, conn: sqlite3.Connection, *, summary: str,
                       ts: int | None = None, time_label: str = '',
                       emotion: float = 0.0, salience: str = 'warm',
                       tags: Iterable[str] = (), pinned: bool = False, quote: str = '',
                       source_event_id: str = '', source_kind: str = 'chat_turn',
                       kind: str = 'event', status: str = 'active',
                       score: float = 0.0, conflict_group: str = '') -> dict[str, Any]:
        """在调用方事务内插入锚点；不提交，供冲突创建等复合写入复用。"""
        ts = _now() if ts is None else int(ts)
        anchor_id = f'epi_{uuid.uuid4().hex[:12]}'
        tag_str = ','.join(sorted({t.strip() for t in tags if t and t.strip()}))
        created = _now()
        quote = (quote or summary or '').strip()[:1200]
        if kind not in MEMORY_KINDS:
            kind = 'event'
        if status not in MEMORY_STATUSES:
            status = 'active'
        source_event_id = (source_event_id or '').strip()
        if source_event_id and not conflict_group:
            existing = conn.execute(
                _ANCHOR_EVENT_KIND_LOAD_SQL,
                (source_event_id, source_kind)).fetchone()
            if existing:
                return dict(existing)
        if status in {'active', 'pending'}:
            self._ensure_capacity(conn)
        tail_id = self._meta(conn, 'tail')
        conn.execute(
            'INSERT INTO anchors(id, ts, time_label, summary, quote, source_event_id, '
            'source_kind, kind, status, conflict_group, score, confirm_count, '
            'last_confirmed_at, updated_at, emotion, salience, prev_id, next_id, tags, '
            'recall_count, last_recalled, pinned, created_at) '
            'VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (anchor_id, ts, time_label, summary, quote, source_event_id, source_kind,
             kind, status, conflict_group, float(score), 0, created, created,
             emotion, salience, tail_id or None, None, tag_str, 0, 0,
             1 if pinned else 0, created),
        )
        if tail_id:
            conn.execute('UPDATE anchors SET next_id=? WHERE id=?',
                         (anchor_id, tail_id))
        self._set_meta(conn, 'tail', anchor_id)
        if not self._meta(conn, 'head'):
            self._set_meta(conn, 'head', anchor_id)
        self._index_anchor(conn, anchor_id, summary, tag_str)
        row = conn.execute('SELECT * FROM anchors WHERE id=?', (anchor_id,)).fetchone()
        return dict(row) if row else {}

    def _index_anchor(self, conn: sqlite3.Connection, anchor_id: str,
                      summary: str, tags: str) -> None:
        if self._fts_mode == 'none':
            return
        conn.execute('INSERT INTO anchors_fts(id, summary, tags) VALUES(?,?,?)',
                     (anchor_id, summary, tags))

    def _unindex_anchor(self, conn: sqlite3.Connection, anchor_id: str) -> None:
        if self._fts_mode == 'none':
            return
        conn.execute('DELETE FROM anchors_fts WHERE id=?', (anchor_id,))

    def get_anchor(self, anchor_id: str) -> dict[str, Any] | None:
        with self._lock, self._session() as conn:
            row = conn.execute('SELECT * FROM anchors WHERE id=?', (anchor_id,)).fetchone()
        return dict(row) if row else None

    def find_anchor_by_event(self, source_event_id: str,
                             source_kind: str = '') -> dict[str, Any] | None:
        """按宿主事件 id 回查锚点；用于 observe 失败重试的幂等保护。"""
        source_event_id = (source_event_id or '').strip()
        if not source_event_id:
            return None
        with self._lock, self._session() as conn:
            sql = _ANCHOR_EVENT_FIND_SQL
            params: list[Any] = [source_event_id]
            if source_kind:
                sql = _ANCHOR_EVENT_FIND_KIND_SQL
                params.append(source_kind)
            row = conn.execute(sql, params).fetchone()
        return dict(row) if row else None

    def semantics_for_event(self, source_event_id: str,
                            source_kind: str = '') -> list[dict[str, Any]]:
        source_event_id = (source_event_id or '').strip()
        if not source_event_id:
            return []
        with self._lock, self._session() as conn:
            sql = _semantics_for_event_sql(bool(source_kind))
            params: list[Any] = [source_event_id]
            if source_kind:
                params.append(source_kind)
            params.append(source_event_id)
            if source_kind:
                params.append(source_kind)
            rows = conn.execute(sql, params).fetchall()
        return [dict(row) for row in rows]

    def link_event_semantic(self, source_event_id: str, source_kind: str,
                            semantic_id: str) -> None:
        """把本次事件触碰到的语义记下来，包括被 reconfirm 的旧条目。"""
        source_event_id = (source_event_id or '').strip()
        if not source_event_id or not semantic_id:
            return
        with self._lock, self._session() as conn:
            conn.execute(
                'INSERT OR IGNORE INTO event_semantics('
                'event_id, source_kind, semantic_id, created_at) VALUES(?,?,?,?)',
                (source_event_id, source_kind, semantic_id, _now()))
            self._commit(conn)

    def claim_event(self, source_event_id: str, source_kind: str) -> bool:
        """原子占位：同一宿主事件同一来源只处理一次。"""
        source_event_id = (source_event_id or '').strip()
        if not source_event_id:
            return True
        with self._lock, self._session() as conn:
            cur = conn.execute(
                'INSERT OR IGNORE INTO processed_events(event_id, source_kind, created_at) '
                'VALUES(?,?,?)', (source_event_id, source_kind, _now()))
            self._commit(conn)
            return bool(cur.rowcount)

    def prune_processed_events(self, now: int | None = None, *,
                               keep_days: int = 365,
                               max_events: int = 20000) -> int:
        """清理过期事件索引；有记忆残留的事件继续保留占位，只删空事件和旧关联。"""
        cutoff = (int(time.time()) if now is None else int(now)) - max(1, keep_days) * 86400
        removed = 0
        with self._lock, self._session() as conn:
            # 仍有记忆残留的事件继续占位，避免 TTL 后重投重复写 wm / 重复 reconfirm；
            # 只有既无锚点也无语义的空事件才清理。event_semantics 跟着占位一起清。
            cur = conn.execute(_PRUNE_PROCESSED_SQL, (cutoff,))
            removed += int(cur.rowcount or 0)
            # 占位不是永久账本：超过上限时删最旧的，重投由引擎按已有记忆兜底判重。
            total = int(conn.execute('SELECT COUNT(*) FROM processed_events').fetchone()[0])
            excess = total - max(1, int(max_events))
            if excess > 0:
                cur = conn.execute(
                    'DELETE FROM processed_events WHERE rowid IN ('
                    'SELECT rowid FROM processed_events '
                    'ORDER BY created_at ASC, rowid ASC LIMIT ?)', (excess,))
                removed += int(cur.rowcount or 0)
            conn.execute(
                'DELETE FROM event_semantics WHERE NOT EXISTS ('
                'SELECT 1 FROM processed_events p WHERE '
                'p.event_id=event_semantics.event_id '
                'AND p.source_kind=event_semantics.source_kind)')
            self._commit(conn)
        return removed

    @staticmethod
    def _is_stale(row: sqlite3.Row, cutoffs: tuple[int, int] | None) -> bool:
        if cutoffs is None:
            return True
        if int(row['pinned'] or 0) != 0:
            return False
        archived_cutoff, dormant_cutoff = cutoffs
        return (
            (row['status'] == 'archived'
             and int(row['updated_at'] or 0) < archived_cutoff)
            or (row['status'] == 'dormant'
                and 0 < int(row['last_confirmed_at'] or 0) < dormant_cutoff)
        )

    def _purge_revision_refs(self, conn: sqlite3.Connection, memory_id: str) -> None:
        """永久删除时清掉撤销快照里引用该 id 的正文，避免 quote/content 残留。"""
        self._purge_revision_refs_many(conn, {memory_id})

    def _purge_revision_refs_many(self, conn: sqlite3.Connection,
                                  memory_ids: set[str]) -> None:
        memory_ids = {memory_id for memory_id in memory_ids if memory_id}
        if not memory_ids:
            return
        self._prune_revisions(conn)
        updates: list[tuple[str, tuple[Any, ...]]] = []
        for row in conn.execute(
                'SELECT id, before_json, after_json, undone FROM revisions'):
            changed = False
            kept: list[Any] = []
            for column in ('before_json', 'after_json'):
                try:
                    data = json.loads(row[column] or '[]')
                except (TypeError, ValueError):
                    data = []
                if isinstance(data, list):
                    filtered = [
                        item for item in data
                        if not (isinstance(item, dict)
                                and item.get('id') in memory_ids)
                    ]
                    changed = changed or len(filtered) != len(data)
                    data = filtered
                kept.append(data)
            if not changed:
                continue
            if any(kept):
                updates.append((
                    'UPDATE revisions SET before_json=?, after_json=? WHERE id=?',
                    (json.dumps(kept[0], ensure_ascii=False),
                     json.dumps(kept[1], ensure_ascii=False), row['id']),
                ))
            else:
                # 整条快照只剩被清理的条目：留一个空墓碑并保留原 undone 状态。
                # 未撤销的墓碑会被“撤销上一步”先命中并报“已被清理”，
                # 不会静默回滚更早的无关动作；已撤销的不会重新变成可撤销。
                updates.append((
                    'UPDATE revisions SET before_json=?, after_json=?, undone=? '
                    'WHERE id=?',
                    ('[]', '[]', int(row['undone'] or 0), row['id']),
                ))
        for sql, params in updates:
            conn.execute(sql, params)

    def _prune_revisions(self, conn: sqlite3.Connection) -> None:
        conn.execute(
            'DELETE FROM revisions WHERE id NOT IN ('
            'SELECT id FROM revisions ORDER BY ts DESC, rowid DESC LIMIT ?)',
            (MAX_REVISIONS,))

    def delete_anchor(self, anchor_id: str, *,
                      stale_cutoffs: tuple[int, int] | None = None,
                      clean_revisions: bool = True) -> bool:
        """删除锚点并修复 prev/next 两端；这是链路完整性的唯一入口。

        stale_cutoffs 非空时，删除前在同一事务里复查 pinned/status/时间，
        避免清理选中后条目刚被置顶或确认。
        """
        with self._lock, self._session() as conn:
            row = conn.execute(
                'SELECT id, prev_id, next_id, status, pinned, updated_at, '
                'last_confirmed_at FROM anchors WHERE id=?', (anchor_id,)).fetchone()
            if row is None or not self._is_stale(row, stale_cutoffs):
                return False
            prev_id, next_id = row['prev_id'], row['next_id']
            if prev_id:
                conn.execute('UPDATE anchors SET next_id=? WHERE id=?', (next_id, prev_id))
            else:
                self._set_meta(conn, 'head', next_id or '')
            if next_id:
                conn.execute('UPDATE anchors SET prev_id=? WHERE id=?', (prev_id, next_id))
            else:
                self._set_meta(conn, 'tail', prev_id or '')
            self._unindex_anchor(conn, anchor_id)
            conn.execute('UPDATE semantics SET attach_anchor_id=NULL '
                         'WHERE attach_anchor_id=?', (anchor_id,))
            if clean_revisions:
                self._purge_revision_refs(conn, anchor_id)
            conn.execute('DELETE FROM anchors WHERE id=?', (anchor_id,))
            self._commit(conn)
        self._purge_delete_residue()
        return True

    def delete_semantic(self, semantic_id: str, *,
                        stale_cutoffs: tuple[int, int] | None = None,
                        clean_revisions: bool = True) -> bool:
        """永久删除语义条目，并清掉邻接边与事件关联；不可撤销。"""
        with self._lock, self._session() as conn:
            row = conn.execute(
                'SELECT id, status, pinned, updated_at, last_confirmed_at '
                'FROM semantics WHERE id=?', (semantic_id,)).fetchone()
            if row is None or not self._is_stale(row, stale_cutoffs):
                return False
            conn.execute('DELETE FROM topics_edges WHERE src=? OR dst=?',
                         (semantic_id, semantic_id))
            conn.execute('DELETE FROM event_semantics WHERE semantic_id=?',
                         (semantic_id,))
            if clean_revisions:
                self._purge_revision_refs(conn, semantic_id)
            conn.execute('DELETE FROM semantics WHERE id=?', (semantic_id,))
            self._commit(conn)
        self._purge_delete_residue()
        return True

    def delete_permanent(self, table: str, memory_id: str) -> dict[str, Any]:
        """用户明确要求立即永久删除单条：不走 30/365 天清理等待。"""
        table = self._table(table)
        if table == 'anchors':
            ok = self.delete_anchor(memory_id)
        else:
            ok = self.delete_semantic(memory_id)
        return {'ok': True, 'table': table, 'id': memory_id} if ok \
            else {'ok': False, 'reason': 'not_found'}

    def clear_all(self) -> dict[str, Any]:
        """清空本角色库的全部记忆、工作记忆、关联与撤销快照。不可撤销。"""
        with self.transaction() as conn:
            counts = {
                'anchors': int(conn.execute('SELECT COUNT(*) FROM anchors').fetchone()[0]),
                'semantics': int(conn.execute('SELECT COUNT(*) FROM semantics').fetchone()[0]),
                'working_turns': int(conn.execute('SELECT COUNT(*) FROM wm').fetchone()[0]),
                'revisions': int(conn.execute('SELECT COUNT(*) FROM revisions').fetchone()[0]),
            }
            for name in ('anchors', 'semantics', 'wm', 'topics_edges',
                         'event_semantics', 'processed_events', 'revisions'):
                conn.execute(f'DELETE FROM {name}')
            self._set_meta(conn, 'head', '')
            self._set_meta(conn, 'tail', '')
            self._rebuild_fts(conn)
            self._commit(conn)
        reclaim = self._shrink_file()
        return {'ok': True, 'reclaimed': bool(reclaim['vacuumed']), **counts}

    def _shrink_file(self) -> dict[str, bool]:
        """清空后把文件真正压回去：截断 WAL + VACUUM。

        只回收磁盘空间和残留页，不影响清空结果；被别处占用时记日志留待下次，
        并把失败如实回报给调用方（面板据此提示“已清空但磁盘未回收”）。
        """
        status = {'wal_truncated': False, 'vacuumed': False}
        with self._lock:
            try:
                conn = self._connect()
            except sqlite3.Error as exc:
                _LOG.warning('清空后重连失败: %s', exc)
                return status
            try:
                try:
                    conn.execute('PRAGMA wal_checkpoint(TRUNCATE)')
                    status['wal_truncated'] = True
                except sqlite3.Error as exc:
                    _LOG.warning('清空后 wal checkpoint 失败: %s', exc)
                try:
                    conn.execute('VACUUM')
                    status['vacuumed'] = True
                except sqlite3.Error as exc:
                    _LOG.warning('清空后 vacuum 失败: %s', exc)
            finally:
                conn.close()
        return status

    def purge_stale(self, now: int | None = None, *,
                    archived_days: int = PURGE_ARCHIVED_DAYS,
                    dormant_days: int = PURGE_DORMANT_DAYS) -> dict[str, Any]:
        """清理长期已移除/已休眠的记忆，给容量上限留真实出口。

        只删 `pinned=0` 且超过宽限期的 archived / dormant 条目；active、
        pending 和近期 archived 一律不动。删除不可撤销，调用方应先提示导出。
        """
        now = int(time.time()) if now is None else int(now)
        archived_cutoff = now - max(1, int(archived_days)) * 86400
        dormant_cutoff = now - max(1, int(dormant_days)) * 86400
        cutoffs = (archived_cutoff, dormant_cutoff)
        with self.transaction() as conn:
            anchor_ids = [row['id'] for row in conn.execute(
                'SELECT id FROM anchors WHERE pinned=0 AND ('
                "(status='archived' AND updated_at < ?) OR "
                "(status='dormant' AND last_confirmed_at > 0 "
                'AND last_confirmed_at < ?))',
                cutoffs).fetchall()]
            semantic_ids = [row['id'] for row in conn.execute(
                'SELECT id FROM semantics WHERE pinned=0 AND ('
                "(status='archived' AND updated_at < ?) OR "
                "(status='dormant' AND last_confirmed_at > 0 "
                'AND last_confirmed_at < ?))',
                cutoffs).fetchall()]
            deleted_anchor_ids = [
                anchor_id for anchor_id in anchor_ids
                if self.delete_anchor(anchor_id, stale_cutoffs=cutoffs,
                                      clean_revisions=False)
            ]
            deleted_semantic_ids = [
                semantic_id for semantic_id in semantic_ids
                if self.delete_semantic(semantic_id, stale_cutoffs=cutoffs,
                                        clean_revisions=False)
            ]
            self._purge_revision_refs_many(
                conn, {*deleted_anchor_ids, *deleted_semantic_ids})
            deleted_anchors = len(deleted_anchor_ids)
            deleted_semantics = len(deleted_semantic_ids)
        return {'ok': True, 'anchors': deleted_anchors,
                'semantics': deleted_semantics,
                'total': deleted_anchors + deleted_semantics}

    def iter_chain(self, start_id: str | None = None, limit: int = 10000,
                   ascending: bool = True) -> list[dict[str, Any]]:
        """从 head/tail 或指定锚点遍历；顺向走 next，逆向走 prev。"""
        with self._lock, self._session() as conn:
            cursor = start_id or self._meta(conn, 'head' if ascending else 'tail')
            out: list[dict[str, Any]] = []
            seen: set[str] = set()
            while cursor and len(out) < limit:
                if cursor in seen:
                    break
                seen.add(cursor)
                row = conn.execute(
                    'SELECT id, ts, summary, prev_id, next_id, salience '
                    'FROM anchors WHERE id=?', (cursor,)).fetchone()
                if row is None:
                    break
                out.append(dict(row))
                cursor = row['next_id'] if ascending else row['prev_id']
        return out

    def chain_integrity(self) -> dict[str, Any]:
        """检测断链：head 无 prev、tail 无 next、中间节点指针不互指。"""
        with self._lock, self._session() as conn:
            head = self._meta(conn, 'head')
            tail = self._meta(conn, 'tail')
            rows = {r['id']: dict(r) for r in conn.execute(
                'SELECT id, prev_id, next_id FROM anchors').fetchall()}
        problems: list[str] = []
        for anchor_id, row in rows.items():
            if row['prev_id'] and rows.get(row['prev_id'], {}).get('next_id') != anchor_id:
                problems.append(f'{anchor_id}: prev 未回指')
            if row['next_id'] and rows.get(row['next_id'], {}).get('prev_id') != anchor_id:
                problems.append(f'{anchor_id}: next 未回指')
        if head and rows.get(head, {}).get('prev_id') is not None:
            problems.append('head 有 prev')
        if tail and rows.get(tail, {}).get('next_id') is not None:
            problems.append('tail 有 next')
        return {'node_count': len(rows), 'head': head, 'tail': tail,
                'problems': problems, 'ok': not problems}

    # ---------------------------------------------------------------- 检索

    def search_anchors(self, query: str, limit: int = 6,
                       exclude_ids: Iterable[str] = ()) -> list[dict[str, Any]]:
        query = (query or '').strip()
        if not query:
            return []
        tokens = _query_tokens(query)
        if not tokens:
            return []
        exclude = set(exclude_ids)
        candidates: dict[str, dict[str, Any]] = {}
        fetch_limit = min(400, max(limit * 8, limit + len(exclude)))
        with self._lock, self._session() as conn:
            fts_tokens = [t for t in tokens if len(t) >= 3]
            if self._fts_mode != 'none' and fts_tokens:
                try:
                    rows = conn.execute(
                        'SELECT a.* FROM anchors_fts f JOIN anchors a ON a.id = f.id '
                        "WHERE anchors_fts MATCH ? AND a.status='active' "
                        'ORDER BY bm25(anchors_fts) LIMIT ?',
                        (self._fts_query(fts_tokens), fetch_limit),
                    ).fetchall()
                    for row in rows:
                        item = dict(row)
                        if item['id'] not in exclude:
                            candidates[item['id']] = item
                except sqlite3.OperationalError:
                    pass
            score_sql, score_params = self._score_terms(tokens, 'summary', 'tags')
            rows = conn.execute(
                f'SELECT * FROM (SELECT anchors.*, ({score_sql}) AS match_score FROM anchors '
                f"WHERE status='active') WHERE match_score > 0 "
                'ORDER BY match_score DESC, ts DESC LIMIT ?',
                (*score_params, fetch_limit),
            ).fetchall()
            for row in rows:
                item = dict(row)
                if item['id'] not in exclude:
                    candidates.setdefault(item['id'], item)
        # 相关度排序：领域词/ASCII/长 token 权重高于汉字二元组，同分再按时间新→旧
        ranked = sorted(candidates.values(), key=lambda a: self._anchor_score(a, tokens),
                        reverse=True)
        return ranked[:limit]

    @staticmethod
    def _token_weight(token: str) -> float:
        if token in DOMAIN_TERMS or token.isascii():
            return 2.0
        return 1.0 if len(token) >= 3 else 0.5

    def _score_terms(self, tokens: list[str], *fields: str) -> tuple[str, list[Any]]:
        """SQL 相关度表达式：在库里先按命中权重排序，避免候选窗口漏掉老而相关的行。"""
        parts: list[str] = []
        params: list[Any] = []
        for token in tokens:
            weight = self._token_weight(token)
            cond = ' OR '.join(f'instr(LOWER({field}), LOWER(?)) > 0' for field in fields)
            parts.append(f'(CASE WHEN {cond} THEN {weight} ELSE 0 END)')
            params.extend([token] * len(fields))
        return ' + '.join(parts) or '0', params

    @staticmethod
    def _anchor_score(anchor: dict[str, Any], tokens: list[str]) -> tuple[float, int]:
        haystack = f'{anchor.get("summary", "")} {anchor.get("tags", "")}'.lower()
        score = 0.0
        for token in tokens:
            if token in haystack:
                if token in DOMAIN_TERMS or token.isascii():
                    score += 2.0
                elif len(token) >= 3:
                    score += 1.0
                else:
                    score += 0.5
        tier = {'hot': 1.15, 'warm': 1.0, 'cold': 0.85}.get(
            str(anchor.get('salience') or 'warm'), 1.0)
        return (round(score * tier, 4), int(anchor.get('ts', 0)))

    def _fts_query(self, tokens: list[str]) -> str:
        # trigram 按词匹配；逐词加引号，词间 OR，避免整句短语零命中
        return ' OR '.join('"' + t.replace('"', '""') + '"' for t in tokens)

    def nearby_anchors(self, ts: int, *, exclude_ids: Iterable[str] = (),
                       ts_radius: int = 86400, limit: int = 4) -> list[dict[str, Any]]:
        exclude = set(exclude_ids)
        with self._lock, self._session() as conn:
            rows = conn.execute(
                "SELECT * FROM anchors WHERE ts BETWEEN ? AND ? AND status='active' "
                'ORDER BY ts ASC LIMIT ?',
                (ts - ts_radius, ts + ts_radius, limit + len(exclude)),
            ).fetchall()
        return [dict(r) for r in rows if r['id'] not in exclude][:limit]

    def bump_recall(self, anchor_ids: Iterable[str]) -> None:
        # 去重：同一锚点在一轮里出现两次不能算两次召回，否则一次召回就满足 hot
        ids = list(dict.fromkeys(a for a in anchor_ids if a))
        if not ids:
            return
        now = _now()
        with self._lock, self._session() as conn:
            for anchor_id in ids:
                conn.execute(
                    'UPDATE anchors SET recall_count = recall_count + 1, last_recalled=? '
                    'WHERE id=?', (now, anchor_id))
            self._commit(conn)

    def tier_stats(self) -> dict[str, int]:
        with self._lock, self._session() as conn:
            rows = conn.execute(
                'SELECT salience, COUNT(*) AS n FROM anchors GROUP BY salience').fetchall()
        return {r['salience']: r['n'] for r in rows}

    def update_salience(self, anchor_id: str, salience: str) -> None:
        with self._lock, self._session() as conn:
            conn.execute('UPDATE anchors SET salience=? WHERE id=?', (salience, anchor_id))
            self._commit(conn)

    def retier(self, now: int, *, hot_window: int, cold_after: int) -> dict[str, int]:
        """单连接批量重算热温冷；新建 hot 在 hot_window 内不被第一次维护降级。"""
        moved = {'hot': 0, 'warm': 0, 'cold': 0}
        with self._lock, self._session() as conn:
            rows = conn.execute(
                'SELECT id, ts, created_at, salience, recall_count, last_recalled, '
                'emotion, pinned, status FROM anchors').fetchall()
            updates: list[tuple[str, str]] = []
            for row in rows:
                if row['pinned'] or row['status'] != 'active':
                    continue
                age = now - int(row['ts'] or 0)
                created_age = now - int(row['created_at'] or row['ts'] or 0)
                if row['salience'] == 'hot' and created_age <= hot_window:
                    target = 'hot'
                elif row['recall_count'] >= 2 and now - int(row['last_recalled'] or 0) <= hot_window:
                    target = 'hot'
                elif row['recall_count'] == 0 and age >= cold_after \
                        and float(row['emotion'] or 0.0) < 0.3:
                    target = 'cold'
                else:
                    target = 'warm'
                if target != row['salience']:
                    updates.append((target, row['id']))
                    moved[target] += 1
            if updates:
                conn.executemany('UPDATE anchors SET salience=? WHERE id=?', updates)
            self._commit(conn)
        return moved

    def all_anchors(self) -> list[dict[str, Any]]:
        with self._lock, self._session() as conn:
            rows = conn.execute('SELECT * FROM anchors ORDER BY ts ASC').fetchall()
        return [dict(r) for r in rows]

    def anchors_between(self, start_ts: int, end_ts: int,
                        limit: int = 30, newest_first: bool = True,
                        include_all: bool = False) -> list[dict[str, Any]]:
        """按 ts 区间取锚点，走 (ts) 索引，不扫全表。

        newest_first=True 时先取区间内最新的 limit 条，再升序返回，
        这样「最近这周」不会被区间开头的旧锚点占满。
        """
        where = '' if include_all else " AND status='active'"
        with self._lock, self._session() as conn:
            rows = conn.execute(
                f'SELECT * FROM anchors WHERE ts BETWEEN ? AND ?{where} '
                'ORDER BY ts DESC LIMIT ?',
                (int(start_ts), int(end_ts), max(1, int(limit))),
            ).fetchall()
        out = [dict(r) for r in rows]
        return list(reversed(out)) if newest_first else out

    # ---------------------------------------------------------------- 语义

    def add_semantic(self, content: str, *, tags: Iterable[str] = (),
                     attach_anchor_id: str | None = None,
                     quote: str = '', source_event_id: str = '',
                     source_kind: str = 'chat_turn', kind: str = 'fact',
                     status: str = 'active', score: float = 0.0,
                     conflict_group: str = '') -> dict[str, Any]:
        with self._lock, self._session() as conn:
            item = self._insert_semantic(
                conn, content, tags=tags, attach_anchor_id=attach_anchor_id,
                quote=quote, source_event_id=source_event_id, source_kind=source_kind,
                kind=kind, status=status, score=score, conflict_group=conflict_group)
            self._commit(conn)
        return item

    def _insert_semantic(self, conn: sqlite3.Connection, content: str, *,
                         tags: Iterable[str] = (), attach_anchor_id: str | None = None,
                         quote: str = '', source_event_id: str = '',
                         source_kind: str = 'chat_turn', kind: str = 'fact',
                         status: str = 'active', score: float = 0.0,
                         conflict_group: str = '') -> dict[str, Any]:
        """在调用方事务内插入语义；不提交，供冲突创建等复合写入复用。"""
        content = (content or '').strip()
        if not content:
            return {}
        semantic_id = f'sem_{uuid.uuid4().hex[:12]}'
        tag_list = sorted({t.strip() for t in tags if t and t.strip()})
        created = _now()
        quote = (quote or content).strip()[:1200]
        if kind not in MEMORY_KINDS:
            kind = 'fact'
        if status not in MEMORY_STATUSES:
            status = 'active'
        source_event_id = (source_event_id or '').strip()
        if source_event_id and not conflict_group:
            existing = conn.execute(
                _SEMANTIC_EVENT_CONTENT_SQL,
                (source_event_id, source_kind, content)).fetchone()
            if existing:
                return dict(existing)
        if status in {'active', 'pending'}:
            self._ensure_capacity(conn)
        conn.execute(
            'INSERT INTO semantics(id, content, quote, source_event_id, source_kind, '
            'kind, status, conflict_group, score, confirm_count, last_confirmed_at, '
            'updated_at, tags, attach_anchor_id, recall_count, last_recalled, pinned, '
            'created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
            (semantic_id, content, quote, source_event_id, source_kind, kind,
             status, conflict_group, float(score), 0, created, created,
             ','.join(tag_list), attach_anchor_id, 0, 0, 0, created),
        )
        self._link_semantic(conn, semantic_id, tag_list)
        row = conn.execute('SELECT * FROM semantics WHERE id=?', (semantic_id,)).fetchone()
        return dict(row) if row else {
            'id': semantic_id, 'content': content, 'kind': kind,
            'status': status, 'tags': tag_list, 'quote': quote,
            'conflict_group': conflict_group, 'score': float(score)}

    def _link_semantic(self, conn: sqlite3.Connection, semantic_id: str,
                       tags: list[str]) -> None:
        if not tags:
            return
        candidates: dict[str, sqlite3.Row] = {}
        tag_list = list(dict.fromkeys(tags))
        # 总量 200 平均分给每个标签，避免前两个标签各占 50 把后面的饿死；
        # 单标签仍受 50 上限约束。
        per_tag_limit = min(MAX_TOPIC_NEIGHBORS,
                            max(1, MAX_TOPIC_TOTAL // max(1, len(tag_list))))
        for tag in tag_list:
            per_tag: dict[str, sqlite3.Row] = {}
            for other in conn.execute(
                    "SELECT id, tags FROM semantics WHERE id != ? "
                    "AND instr(',' || tags || ',', ',' || ? || ',') > 0 "
                    'ORDER BY created_at DESC LIMIT ?',
                    (semantic_id, tag, per_tag_limit)).fetchall():
                per_tag[other['id']] = other
            for other_id, other in per_tag.items():
                candidates.setdefault(other_id, other)
        for other in candidates.values():
            shared = set(tags) & set(_split_tags(other['tags']))
            if not shared:
                continue
            union = set(tags) | set(_split_tags(other['tags']))
            weight = len(shared) / max(1, len(union))
            conn.execute(
                'INSERT INTO topics_edges(src, dst, weight) VALUES(?,?,?) '
                'ON CONFLICT(src, dst) DO UPDATE SET weight=excluded.weight',
                (semantic_id, other['id'], round(weight, 3)),
            )
            conn.execute(
                'INSERT INTO topics_edges(src, dst, weight) VALUES(?,?,?) '
                'ON CONFLICT(src, dst) DO UPDATE SET weight=excluded.weight',
                (other['id'], semantic_id, round(weight, 3)),
            )

    def search_semantics(self, query: str, limit: int = 8) -> list[dict[str, Any]]:
        query = (query or '').strip()
        if not query:
            return []
        tokens = _query_tokens(query)
        if not tokens:
            return []
        score_sql, score_params = self._score_terms(tokens, 'content', 'tags')
        with self._lock, self._session() as conn:
            rows = conn.execute(
                f'SELECT * FROM (SELECT semantics.*, ({score_sql}) AS match_score '
                f"FROM semantics WHERE status='active') WHERE match_score > 0 "
                f'ORDER BY match_score DESC, created_at DESC LIMIT ?',
                (*score_params, max(limit * 4, limit)),
            ).fetchall()
        ranked = sorted((dict(r) for r in rows),
                        key=lambda row: self._semantic_score(row, tokens), reverse=True)
        return ranked[:limit]

    @staticmethod
    def _semantic_score(item: dict[str, Any], tokens: list[str]) -> tuple[float, int]:
        haystack = f'{item.get("content", "")} {item.get("tags", "")}'.lower()
        score = 0.0
        for token in tokens:
            if token in haystack:
                if token in DOMAIN_TERMS or token.isascii():
                    score += 2.0
                elif len(token) >= 3:
                    score += 1.0
                else:
                    score += 0.5
        return (score, int(item.get('created_at', 0)))

    def expand_semantics(self, semantic_ids: Iterable[str], *, hops: int = 2,
                         limit: int = 8) -> list[dict[str, Any]]:
        """从命中节点沿 topics_edges 走最多 hops 跳，限深防止图爆炸。"""
        frontier = [s for s in semantic_ids if s]
        visited: set[str] = set(frontier)
        collected: list[str] = []
        reached_limit = False
        with self._lock, self._session() as conn:
            for _ in range(max(1, hops)):
                if not frontier or reached_limit:
                    break
                next_frontier: list[str] = []
                for node in frontier:
                    for edge in conn.execute(
                            'SELECT dst FROM topics_edges WHERE src=? ORDER BY weight DESC',
                            (node,)).fetchall():
                        dst = edge['dst']
                        if dst in visited:
                            continue
                        visited.add(dst)
                        collected.append(dst)
                        next_frontier.append(dst)
                        if len(collected) >= limit:
                            reached_limit = True
                            break
                    if reached_limit:
                        break
                frontier = next_frontier
            if not collected:
                return []
            placeholders = ','.join('?' for _ in collected)
            rows = conn.execute(
                f"SELECT * FROM semantics WHERE id IN ({placeholders}) AND status='active'",
                collected).fetchall()
        by_id = {row['id']: dict(row) for row in rows}
        return [by_id[i] for i in collected if i in by_id]

    # ---------------------------------------------------------------- 生命周期与纠错

    @staticmethod
    def _table(name: str) -> str:
        if name not in {'anchors', 'semantics'}:
            raise ValueError(f'未知记忆表: {name}')
        return name

    def get_memory(self, table: str, memory_id: str) -> dict[str, Any] | None:
        table = self._table(table)
        with self._lock, self._session() as conn:
            row = conn.execute(f'SELECT * FROM {table} WHERE id=?', (memory_id,)).fetchone()
        return dict(row) if row else None

    def all_memories(self, table: str, limit: int | None = 400,
                     statuses: Iterable[str] | None = None) -> list[dict[str, Any]]:
        table = self._table(table)
        order = ("pinned DESC, (status='active') DESC, ts DESC"
                 if table == 'anchors' else
                 "pinned DESC, (status='active') DESC, created_at DESC")
        wanted = tuple(statuses) if statuses is not None else None
        if wanted is not None and not wanted:
            return []
        sql = f'SELECT * FROM {table} '
        params: list[Any] = []
        if wanted:
            sql += f'WHERE status IN ({",".join("?" for _ in wanted)}) '
            params.extend(wanted)
        sql += f'ORDER BY {order}'
        if limit is not None:
            sql += ' LIMIT ?'
            params.append(max(1, int(limit)))
        with self._lock, self._session() as conn:
            rows = conn.execute(sql, tuple(params)).fetchall()
        return [dict(r) for r in rows]

    def recent_semantics(self, kind: str = '', limit: int = 200) -> list[dict[str, Any]]:
        with self._lock, self._session() as conn:
            if kind:
                rows = conn.execute(
                    'SELECT * FROM semantics WHERE kind=? '
                    'ORDER BY created_at DESC LIMIT ?', (kind, limit)).fetchall()
            else:
                rows = conn.execute(
                    'SELECT * FROM semantics ORDER BY created_at DESC LIMIT ?',
                    (limit,)).fetchall()
        return [dict(r) for r in rows]

    def _record_revision(self, conn: sqlite3.Connection, action: str,
                         before: list[dict[str, Any]], after: list[dict[str, Any]]) -> str:
        revision_id = f'rev_{uuid.uuid4().hex[:12]}'
        conn.execute(
            'INSERT INTO revisions(id, ts, action, before_json, after_json, undone) '
            'VALUES(?,?,?,?,?,0)',
            (revision_id, _now(), action, json.dumps(before, ensure_ascii=False),
             json.dumps(after, ensure_ascii=False)),
        )
        self._prune_revisions(conn)
        return revision_id

    def revise_memory(self, table: str, memory_id: str, *,
                      action: str = 'correct', **fields: Any) -> dict[str, Any]:
        """面板纠正入口：只改白名单字段，并在同一事务里留撤销快照。"""
        table = self._table(table)
        allowed = {'summary', 'content', 'kind', 'conflict_group', 'tags'}
        if 'quote' in fields:
            return {'ok': False, 'reason': 'quote_readonly'}
        updates = {k: v for k, v in fields.items() if k in allowed}
        if not updates:
            return {'ok': False, 'reason': 'no_fields'}
        if 'kind' in updates and updates['kind'] not in MEMORY_KINDS:
            return {'ok': False, 'reason': 'bad_kind'}
        text_column = 'summary' if table == 'anchors' else 'content'
        if text_column in updates:
            text = str(updates[text_column] or '').strip()[:1200]
            if not text:
                return {'ok': False, 'reason': 'empty_text'}
            updates[text_column] = text
        if 'tags' in updates:
            raw_tags = updates['tags']
            values = raw_tags if isinstance(raw_tags, (list, tuple, set)) \
                else str(raw_tags or '').split(',')
            updates['tags'] = ','.join(sorted({
                str(tag).strip() for tag in values if str(tag).strip()}))
        updates['updated_at'] = _now()
        with self.transaction():
            # 旧行与快照必须在同一事务里读，避免快照漏掉并发线程刚做的修改。
            row = self.get_memory(table, memory_id)
            if row is None:
                return {'ok': False, 'reason': 'not_found'}
            if row.get('status') == 'dormant':
                # 面板纠正等于重新核对过内容，顺手唤醒，否则改完仍然不可召回。
                updates['status'] = 'active'
                updates['last_confirmed_at'] = updates['updated_at']
            assignments = ', '.join(f'{key}=?' for key in updates)
            with self._lock, self._session() as conn:
                conn.execute(f'UPDATE {table} SET {assignments} WHERE id=?',
                             (*updates.values(), memory_id))
                after = dict(row)
                after.update(updates)
                revision = self._record_revision(conn, action, [row], [after])
                self._commit(conn)
            # 索引/邻接边与字段改动同一事务，避免提交后重建失败留下旧边。
            if table == 'anchors' and ('summary' in updates or 'tags' in updates):
                self._reindex_anchor(memory_id)
            elif table == 'semantics' and 'tags' in updates:
                self._rebuild_semantic_links(memory_id)
        return {'ok': True, 'revision': revision, 'item': after}

    def _rebuild_semantic_links(self, semantic_id: str) -> None:
        """标签被纠正后重建邻接边，避免 FTS/边表与条目内容脱节。"""
        with self._lock, self._session() as conn:
            row = conn.execute('SELECT tags FROM semantics WHERE id=?', (semantic_id,)).fetchone()
            if row is None:
                return
            conn.execute('DELETE FROM topics_edges WHERE src=? OR dst=?',
                         (semantic_id, semantic_id))
            self._link_semantic(conn, semantic_id, _split_tags(row['tags']))
            self._commit(conn)

    def _reindex_anchor(self, anchor_id: str) -> None:
        with self._lock, self._session() as conn:
            row = conn.execute('SELECT summary, tags FROM anchors WHERE id=?',
                               (anchor_id,)).fetchone()
            if row is None or self._fts_mode == 'none':
                return
            conn.execute('DELETE FROM anchors_fts WHERE id=?', (anchor_id,))
            conn.execute('INSERT INTO anchors_fts(id, summary, tags) VALUES(?,?,?)',
                         (anchor_id, row['summary'], row['tags']))
            self._commit(conn)

    def set_memory_state(self, table: str, memory_id: str, *,
                         status: str | None = None,
                         pinned: bool | None = None,
                         confirm: bool = False,
                         action: str = 'state',
                         record_revision: bool = True,
                         allow_pending: bool = False,
                         require_orphan: bool = False) -> dict[str, Any]:
        table = self._table(table)
        if status is not None and status not in MEMORY_STATUSES:
            return {'ok': False, 'reason': 'bad_status'}
        with self.transaction() as conn:
            row = self.get_memory(table, memory_id)
            if row is None:
                return {'ok': False, 'reason': 'not_found'}
            if require_orphan and (row.get('status') != 'pending'
                                  or row.get('conflict_group')):
                return {'ok': False, 'reason': 'not_orphan'}
            if row.get('status') == 'pending' and not allow_pending:
                return {'ok': False, 'reason': 'resolve_conflict_first'}
            now = _now()
            updates: dict[str, Any] = {'updated_at': now}
            if status is not None:
                updates['status'] = status
            if pinned is not None:
                updates['pinned'] = 1 if pinned else 0
                if pinned:
                    updates['status'] = 'active' if status is None else status
                    updates['last_confirmed_at'] = now
            if confirm:
                updates['status'] = 'active' if status is None else status
                updates['last_confirmed_at'] = now
                updates['confirm_count'] = int(row.get('confirm_count') or 0) + 1
            next_status = updates.get('status', row.get('status'))
            if (next_status in {'active', 'pending'}
                    and row.get('status') not in {'active', 'pending'}):
                try:
                    self._ensure_capacity(conn)
                except MemoryCapacityError:
                    return {'ok': False, 'reason': 'capacity'}
            assignments = ', '.join(f'{key}=?' for key in updates)
            conn.execute(f'UPDATE {table} SET {assignments} WHERE id=?',
                         (*updates.values(), memory_id))
            after = dict(row)
            after.update(updates)
            revision = self._record_revision(conn, action, [row], [after]) \
                if record_revision else ''
            self._commit(conn)
        # 索引/邻接边与字段改动同一事务，避免提交后重建失败留下旧边。
        if table == 'anchors' and ('summary' in updates or 'tags' in updates):
            self._reindex_anchor(memory_id)
        elif table == 'semantics' and 'tags' in updates:
            self._rebuild_semantic_links(memory_id)
        return {'ok': True, 'revision': revision, 'item': after}

    def confirm_memory(self, table: str, memory_id: str) -> dict[str, Any]:
        return self.set_memory_state(table, memory_id, confirm=True, action='confirm')

    def set_pinned(self, table: str, memory_id: str, pinned: bool) -> dict[str, Any]:
        return self.set_memory_state(table, memory_id, pinned=pinned, action='pin')

    def resolve_orphan_pending(self, table: str, memory_id: str,
                               keep: bool = True) -> dict[str, Any]:
        """处理没有冲突组的 pending：保留转 active，否则归档。"""
        return self.set_memory_state(
            table, memory_id,
            status='active' if keep else 'archived',
            confirm=keep, action='resolve_orphan', allow_pending=True,
            require_orphan=True,
        )

    def archive_memory(self, table: str, memory_id: str) -> dict[str, Any]:
        return self.set_memory_state(table, memory_id, status='archived', action='archive')

    def lifecycle(self, now: int | None = None, *, event_days: int = 90,
                  preference_days: int = 180) -> dict[str, int]:
        """久未确认自动休眠；手动固定永不自动休眠。"""
        now = _now() if now is None else int(now)
        event_cut = now - max(1, int(event_days)) * 86400
        pref_cut = now - max(1, int(preference_days)) * 86400
        moved = {'anchors': 0, 'semantics': 0}
        with self._lock, self._session() as conn:
            cur = conn.execute(
                "UPDATE anchors SET status='dormant', updated_at=? "
                "WHERE status='active' AND pinned=0 AND kind='event' "
                'AND last_confirmed_at > 0 AND last_confirmed_at < ?',
                (now, event_cut),
            )
            moved['anchors'] = int(cur.rowcount or 0)
            cur = conn.execute(
                "UPDATE anchors SET status='dormant', updated_at=? "
                "WHERE status='active' AND pinned=0 AND kind!='event' "
                'AND last_confirmed_at > 0 AND last_confirmed_at < ?',
                (now, pref_cut),
            )
            moved['anchors'] += int(cur.rowcount or 0)
            cur = conn.execute(
                "UPDATE semantics SET status='dormant', updated_at=? "
                "WHERE status='active' AND pinned=0 AND kind='event' "
                'AND last_confirmed_at > 0 AND last_confirmed_at < ?',
                (now, event_cut),
            )
            moved['semantics'] = int(cur.rowcount or 0)
            cur = conn.execute(
                "UPDATE semantics SET status='dormant', updated_at=? "
                "WHERE status='active' AND pinned=0 AND kind!='event' "
                'AND last_confirmed_at > 0 AND last_confirmed_at < ?',
                (now, pref_cut),
            )
            moved['semantics'] += int(cur.rowcount or 0)
            self._commit(conn)
        return moved

    def pending_conflicts(self) -> list[dict[str, Any]]:
        with self._lock, self._session() as conn:
            rows = conn.execute(
                "SELECT id, ts, summary AS content, quote, kind, conflict_group, "
                "'anchors' AS table_name FROM anchors "
                "WHERE status='pending' AND conflict_group!='' "
                'UNION ALL '
                "SELECT id, created_at AS ts, content, quote, kind, conflict_group, "
                "'semantics' AS table_name FROM semantics "
                "WHERE status='pending' AND conflict_group!='' "
                'ORDER BY conflict_group, ts ASC',
            ).fetchall()
        return [dict(r) for r in rows]

    def orphan_pending(self, limit: int = 120) -> list[dict[str, Any]]:
        """没有冲突组的 pending 条目：面板必须能看见并处理，否则只占容量。"""
        with self._lock, self._session() as conn:
            rows = conn.execute(
                "SELECT id, ts, summary AS content, quote, kind, '' AS conflict_group, "
                "'anchors' AS table_name FROM anchors "
                "WHERE status='pending' AND conflict_group='' "
                'UNION ALL '
                "SELECT id, created_at AS ts, content, quote, kind, '' AS conflict_group, "
                "'semantics' AS table_name FROM semantics "
                "WHERE status='pending' AND conflict_group='' "
                'ORDER BY ts ASC LIMIT ?',
                (max(1, int(limit)),),
            ).fetchall()
        return [dict(r) for r in rows]

    def resolve_conflict(self, group: str, keep_id: str) -> dict[str, Any]:
        if not group:
            return {'ok': False, 'reason': 'empty_group'}
        rows = []
        with self._lock, self._session() as conn:
            for table in ('anchors', 'semantics'):
                for row in conn.execute(
                        f"SELECT * FROM {table} WHERE conflict_group=?",
                        (group,)).fetchall():
                    item = dict(row)
                    item['_table'] = table
                    rows.append(item)
            if not rows:
                return {'ok': False, 'reason': 'not_found'}
            if keep_id not in {r['id'] for r in rows}:
                return {'ok': False, 'reason': 'keep_not_in_group'}
            now = _now()
            before = [{k: v for k, v in r.items() if k != '_table'} for r in rows]
            after = []
            for row in rows:
                status = 'active' if row['id'] == keep_id else 'archived'
                confirmed = now if row['id'] == keep_id else int(row.get('last_confirmed_at') or 0)
                conn.execute(
                    'UPDATE ' + row['_table'] + " SET status=?, last_confirmed_at=?, updated_at=? "
                    "WHERE id=?", (status, confirmed, now, row['id']))
                item = {k: v for k, v in row.items() if k != '_table'}
                item.update({'status': status, 'last_confirmed_at': confirmed, 'updated_at': now})
                after.append(item)
            revision = self._record_revision(conn, 'resolve_conflict', before, after)
            self._commit(conn)
        return {'ok': True, 'revision': revision, 'keep_id': keep_id}

    def _existing_orphan(self, conn: sqlite3.Connection, table: str,
                         new_item: dict[str, Any], quote: str) -> dict[str, Any] | None:
        """找还没有冲突组的同一条待确认条目，避免反复反驳堆满孤儿。"""
        if table == 'semantics':
            content = str(new_item.get('content') or '').strip()
            if not content:
                return None
            row = conn.execute(
                "SELECT * FROM semantics WHERE status='pending' AND conflict_group='' "
                'AND content=? ORDER BY created_at DESC LIMIT 1', (content,)).fetchone()
        else:
            summary = str(new_item.get('summary') or '').strip()
            if not summary:
                return None
            row = conn.execute(
                "SELECT * FROM anchors WHERE status='pending' AND conflict_group='' "
                'AND summary=? AND quote=? ORDER BY ts DESC LIMIT 1',
                (summary, quote)).fetchone()
        return dict(row) if row else None

    def materialize_conflict(self, table: str, old_id: str, new_item: dict[str, Any],
                             group: str, *, quote: str, source_event_id: str = '',
                             kind: str, score: float = 0.0) -> dict[str, Any]:
        """把新旧两条都标成待确认；新条只作为证据候选，不直接进召回。"""
        table = self._table(table)
        with self._lock, self._session() as conn:
            old = conn.execute(f'SELECT * FROM {table} WHERE id=?', (old_id,)).fetchone()
            if old is None:
                return {'ok': False, 'reason': 'not_found'}
            old = dict(old)
            now = _now()
            if old.get('pinned'):
                existing = self._existing_orphan(conn, table, new_item, quote)
                if existing is not None:
                    # 固定旧条被反复反驳：同一条新说法不再重复堆未分组待确认
                    return {'ok': True, 'old': old, 'new': existing, 'group': '',
                            'pinned_old': True, 'duplicate': True}
            if table == 'anchors':
                new_row = self._insert_anchor(
                    conn, summary=str(new_item.get('summary') or ''), ts=now,
                    time_label=str(new_item.get('time_label') or ''),
                    tags=new_item.get('tags') or (), quote=quote,
                    source_event_id=source_event_id,
                    source_kind=str(new_item.get('source_kind') or 'chat_turn'),
                    kind=kind, status='pending', score=score, conflict_group=group)
            else:
                new_row = self._insert_semantic(
                    conn, str(new_item.get('content') or ''),
                    tags=new_item.get('tags') or (),
                    attach_anchor_id=new_item.get('attach_anchor_id'), quote=quote,
                    source_event_id=source_event_id,
                    source_kind=str(new_item.get('source_kind') or 'chat_turn'),
                    kind=kind, status='pending', score=score, conflict_group=group)
            if not new_row:
                return {'ok': False, 'reason': 'empty_new_item'}
            new_before = {**new_row, 'status': 'archived', 'conflict_group': '',
                          '_table': table}
            pinned_old = bool(old.get('pinned'))
            if pinned_old:
                # 已固定是用户明确要求保留的结论：不冻结旧条，只把新说法挂成未分组待确认
                conn.execute(f'UPDATE {table} SET conflict_group=?, updated_at=? WHERE id=?',
                             ('', now, new_row['id']))
                old_after = {**old, '_table': table}
                new_after = {**new_row, 'conflict_group': '', '_table': table}
            else:
                conn.execute(
                    f'UPDATE {table} SET status=?, conflict_group=?, updated_at=? WHERE id=?',
                    ('pending', group, now, old_id))
                old_after = {**old, 'status': 'pending', 'conflict_group': group,
                             'updated_at': now, '_table': table}
                new_after = {**new_row, '_table': table}
            self._record_revision(conn, 'conflict_create',
                                  [{**old, '_table': table}, new_before],
                                  [old_after, new_after])
            self._commit(conn)
        returned = ({**new_row, 'conflict_group': ''} if pinned_old else new_row)
        return {'ok': True, 'old': old, 'new': returned,
                'group': '' if pinned_old else group, 'pinned_old': pinned_old}

    def last_revision(self) -> dict[str, Any] | None:
        with self._lock, self._session() as conn:
            row = conn.execute(
                'SELECT id, ts, action FROM revisions WHERE undone=0 '
                'ORDER BY ts DESC, rowid DESC LIMIT 1').fetchone()
        return dict(row) if row else None

    def undo_last(self) -> dict[str, Any]:
        with self.transaction():
            with self._lock, self._session() as conn:
                revision = conn.execute(
                    'SELECT * FROM revisions WHERE undone=0 '
                    'ORDER BY ts DESC, rowid DESC LIMIT 1'
                ).fetchone()
                if revision is None:
                    return {'ok': False, 'reason': 'nothing_to_undo'}
                before = json.loads(revision['before_json'] or '[]')
                restored = 0
                rebuild_links: list[str] = []
                targets: list[tuple[str, str, dict[str, Any]]] = []
                activations = 0
                for item in before:
                    raw_table = item.get('_table') or (
                        'anchors' if str(item.get('id', '')).startswith('epi_')
                        else 'semantics')
                    try:
                        table = self._table(raw_table)
                    except ValueError:
                        continue
                    memory_id = item.get('id')
                    if not memory_id:
                        continue
                    current = conn.execute(
                        f'SELECT status FROM {table} WHERE id=?', (memory_id,)).fetchone()
                    if current is None:
                        continue
                    fields = {k: v for k, v in item.items()
                              if k not in {'id', '_table'} and k in {
                                  'summary', 'content', 'quote', 'kind', 'status', 'pinned',
                                  'conflict_group', 'confirm_count', 'last_confirmed_at',
                                  'updated_at', 'tags', 'score'}}
                    if not fields:
                        continue
                    if (fields.get('status') in {'active', 'pending'}
                            and current['status'] not in {'active', 'pending'}):
                        activations += 1
                    targets.append((table, memory_id, fields))
                if activations:
                    try:
                        self._ensure_capacity(conn, extra=activations)
                    except MemoryCapacityError:
                        return {'ok': False, 'reason': 'capacity', 'restored': 0}
                for table, memory_id, fields in targets:
                    assignments = ', '.join(f'{key}=?' for key in fields)
                    cur = conn.execute(f'UPDATE {table} SET {assignments} WHERE id=?',
                                       (*fields.values(), memory_id))
                    if not cur.rowcount:
                        continue
                    if table == 'semantics' and 'tags' in fields:
                        rebuild_links.append(memory_id)
                    restored += 1
                conn.execute('UPDATE revisions SET undone=1 WHERE id=?', (revision['id'],))
                self._commit(conn)
                if restored == 0:
                    return {'ok': False, 'reason': 'target_missing', 'restored': 0}
                out = {'ok': True, 'action': revision['action'], 'restored': restored,
                       'revision': revision['id']}
            # 字段回滚、FTS 重建、邻接边重建同一事务；中途失败不会留下字段新、索引旧。
            self.reindex_all()
            for semantic_id in dict.fromkeys(rebuild_links):
                self._rebuild_semantic_links(semantic_id)
        return out

    def reindex_all(self) -> None:
        if self._fts_mode == 'none':
            return
        with self._lock, self._session() as conn:
            self._rebuild_fts(conn)
            self._commit(conn)

    # ---------------------------------------------------------------- 统计

    def next_turn_no(self) -> int:
        with self._lock, self._session() as conn:
            row = conn.execute("SELECT v FROM meta WHERE k='turn_no'").fetchone()
            turn = int(row['v']) if row else 0
            turn += 1
            conn.execute(
                "INSERT INTO meta(k, v) VALUES('turn_no', ?) "
                'ON CONFLICT(k) DO UPDATE SET v=excluded.v', (str(turn),))
        return turn

    def stats(self) -> dict[str, Any]:
        # 用户清掉旧库重复行后，面板刷新即可重试建索引，无需重启宿主。
        if self._unique_index_skipped:
            self.retry_unique_indexes()
        with self._lock, self._session() as conn:
            anchors = conn.execute('SELECT COUNT(*) FROM anchors').fetchone()[0]
            semantics = conn.execute('SELECT COUNT(*) FROM semantics').fetchone()[0]
            edges = conn.execute('SELECT COUNT(*) FROM topics_edges').fetchone()[0]
            wm = conn.execute('SELECT COUNT(*) FROM wm').fetchone()[0]
            turn_row = conn.execute("SELECT v FROM meta WHERE k='turn_no'").fetchone()
            status_rows = conn.execute(
                "SELECT status, COUNT(*) AS n FROM ("
                'SELECT status FROM anchors UNION ALL SELECT status FROM semantics'
                ') GROUP BY status').fetchall()
            pending = conn.execute(
                "SELECT COUNT(DISTINCT conflict_group) AS n FROM ("
                "SELECT conflict_group FROM anchors WHERE status='pending' AND conflict_group!='' "
                'UNION ALL '
                "SELECT conflict_group FROM semantics WHERE status='pending' AND conflict_group!=''"
                ')').fetchone()[0]
        return {
            'anchors': anchors,
            'semantics': semantics,
            'edges': edges,
            'working_turns': wm,
            'fts_mode': self._fts_mode,
            'tiers': self.tier_stats(),
            'statuses': {r['status']: r['n'] for r in status_rows},
            'pending_conflicts': int(pending or 0),
            'turn_no': int(turn_row['v']) if turn_row else 0,
            'unique_index_skipped': list(self._unique_index_skipped),
        }

    def close(self) -> None:
        return
