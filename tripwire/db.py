"""SQLite storage (WAL, single file)."""

from __future__ import annotations

import asyncio
import json as _json
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

from .models import Cluster, Event
from .util import from_ts, json_dumps, json_loads, now_ts, to_ts, utcnow

SCHEMA = """
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source_id TEXT NOT NULL,
    tier INTEGER NOT NULL,
    url TEXT,
    guid_hash TEXT NOT NULL,
    title TEXT NOT NULL,
    body_excerpt TEXT,
    lang TEXT,
    published_at INTEGER,
    ingested_at INTEGER NOT NULL,
    category TEXT,
    theater TEXT,
    country TEXT,
    lat REAL,
    lon REAL,
    numeric_payload TEXT,
    simhash INTEGER,
    cluster_id INTEGER,
    score REAL DEFAULT 0,
    level INTEGER DEFAULT 0,
    matched_rules TEXT,
    origin_domain TEXT,
    status TEXT DEFAULT 'new',
    kind TEXT DEFAULT 'event',
    recorded_at INTEGER
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_events_url ON events(url) WHERE url IS NOT NULL AND url <> '';
CREATE INDEX IF NOT EXISTS idx_events_ingested ON events(ingested_at);
CREATE INDEX IF NOT EXISTS idx_events_published ON events(published_at);
CREATE INDEX IF NOT EXISTS idx_events_cluster ON events(cluster_id);
CREATE INDEX IF NOT EXISTS idx_events_source ON events(source_id, ingested_at);
CREATE INDEX IF NOT EXISTS idx_events_theater ON events(theater, ingested_at);
CREATE INDEX IF NOT EXISTS idx_events_guid ON events(guid_hash);

-- Append-only history: every version of an event row with the transaction time
-- (recorded_at) at which the system held it. Valid time is published_at,
-- knowledge time is ingested_at, transaction time is recorded_at here.
CREATE TABLE IF NOT EXISTS event_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    event_id INTEGER NOT NULL,
    recorded_at INTEGER NOT NULL,
    reason TEXT NOT NULL,
    snapshot TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_event_revisions_event ON event_revisions(event_id, recorded_at);

CREATE TABLE IF NOT EXISTS clusters (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    theater TEXT NOT NULL,
    category TEXT NOT NULL,
    title_canonical TEXT,
    first_seen INTEGER NOT NULL,
    last_seen INTEGER NOT NULL,
    sources_count INTEGER DEFAULT 0,
    tiers_bitmap INTEGER DEFAULT 0,
    total_score REAL DEFAULT 0,
    score_initial REAL,
    level INTEGER DEFAULT 0,
    tg_message_id INTEGER,
    tg_thread_id INTEGER,
    state TEXT DEFAULT 'open',
    country TEXT,
    lat REAL,
    lon REAL,
    last_alert_at INTEGER,
    ack_at INTEGER,
    tripwire_id TEXT,
    meta TEXT DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_clusters_open ON clusters(state, theater, last_seen);
CREATE INDEX IF NOT EXISTS idx_clusters_tg ON clusters(tg_message_id);

CREATE TABLE IF NOT EXISTS cluster_sources (
    cluster_id INTEGER NOT NULL,
    source_id TEXT NOT NULL,
    first_seen INTEGER NOT NULL,
    PRIMARY KEY (cluster_id, source_id)
);

CREATE TABLE IF NOT EXISTS theater_state (
    theater TEXT PRIMARY KEY,
    score REAL DEFAULT 0,
    level INTEGER DEFAULT 0,
    level_since INTEGER,
    last_update INTEGER,
    cooldown_until INTEGER,
    muted_until INTEGER
);

CREATE TABLE IF NOT EXISTS alerts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    cluster_id INTEGER,
    level INTEGER NOT NULL,
    sent_at INTEGER NOT NULL,
    tg_message_id INTEGER,
    explain TEXT,
    tripwire_id TEXT,
    ack_at INTEGER,
    status TEXT DEFAULT 'sent',
    decided_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_alerts_cluster ON alerts(cluster_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_alerts_tripwire ON alerts(tripwire_id, sent_at);
CREATE INDEX IF NOT EXISTS idx_alerts_sent ON alerts(sent_at);

CREATE TABLE IF NOT EXISTS source_health (
    source_id TEXT PRIMARY KEY,
    last_ok INTEGER,
    last_error TEXT,
    last_error_at INTEGER,
    consecutive_failures INTEGER DEFAULT 0,
    avg_latency_ms REAL DEFAULT 0,
    etag TEXT,
    last_modified TEXT,
    last_items INTEGER DEFAULT 0,
    last_checked INTEGER,
    first_error_at INTEGER,
    total_fetches INTEGER DEFAULT 0,
    total_errors INTEGER DEFAULT 0,
    total_items INTEGER DEFAULT 0
);

CREATE TABLE IF NOT EXISTS seen (
    hash TEXT PRIMARY KEY,
    kind TEXT,
    seen_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_seen_at ON seen(seen_at);

CREATE TABLE IF NOT EXISTS meta (
    key TEXT PRIMARY KEY,
    value TEXT,
    updated_at INTEGER
);

CREATE TABLE IF NOT EXISTS indicator_state (
    name TEXT PRIMARY KEY,
    value TEXT,
    updated_at INTEGER,
    meta TEXT DEFAULT '{}'
);

CREATE TABLE IF NOT EXISTS market_samples (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol TEXT NOT NULL,
    ts INTEGER NOT NULL,
    close REAL,
    change_pct REAL,
    source_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_market_symbol_ts ON market_samples(symbol, ts);

CREATE TABLE IF NOT EXISTS config_journal (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    actor TEXT,
    action TEXT,
    payload TEXT
);

CREATE TABLE IF NOT EXISTS heartbeats (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts INTEGER NOT NULL,
    component TEXT,
    detail TEXT
);
CREATE INDEX IF NOT EXISTS idx_heartbeats_ts ON heartbeats(ts);

CREATE TABLE IF NOT EXISTS translations (
    hash TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    dst_lang TEXT NOT NULL,
    source TEXT NOT NULL,
    target TEXT NOT NULL,
    created_at INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_translations_created ON translations(created_at);

CREATE TABLE IF NOT EXISTS pending_actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at INTEGER NOT NULL,
    due_at INTEGER NOT NULL,
    kind TEXT NOT NULL,
    payload TEXT DEFAULT '{}',
    status TEXT DEFAULT 'pending'
);
CREATE INDEX IF NOT EXISTS idx_pending_due ON pending_actions(status, due_at);
"""


class DB:
    def __init__(self, path: Path | str) -> None:
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None
        self._lock = asyncio.Lock()

    async def open(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(str(self.path))
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA synchronous=NORMAL")
        await self.conn.execute("PRAGMA busy_timeout=5000")
        await self.conn.execute("PRAGMA wal_autocheckpoint=1000")
        await self.conn.executescript(SCHEMA)
        await self._migrate()
        await self.conn.commit()

    async def _migrate(self) -> None:
        assert self.conn is not None
        cur = await self.conn.execute("PRAGMA table_info(source_health)")
        columns = {row[1] for row in await cur.fetchall()}
        await cur.close()
        if "first_error_at" not in columns:
            await self.conn.execute(
                "ALTER TABLE source_health ADD COLUMN first_error_at INTEGER"
            )
        await self._add_column("events", "recorded_at", "INTEGER")
        await self._add_column("alerts", "decided_at", "INTEGER")
        # Calibration anchor: the theater score after the cluster's first scored
        # event. Final total_score keeps growing, which would leak the future.
        await self._add_column("clusters", "score_initial", "REAL")
        await self.conn.execute(
            "UPDATE events SET recorded_at=ingested_at WHERE recorded_at IS NULL"
        )
        await self.conn.execute(
            "UPDATE alerts SET decided_at=sent_at WHERE decided_at IS NULL"
        )
        # One baseline revision for rows that predate the history table: their
        # pre-migration state is only known as of the migration moment.
        cur = await self.conn.execute("SELECT COUNT(*) FROM event_revisions")
        seeded = (await cur.fetchone())[0]
        await cur.close()
        if not seeded:
            await self.conn.execute(
                """INSERT INTO event_revisions (event_id, recorded_at, reason, snapshot)
                   SELECT id, COALESCE(recorded_at, ingested_at), 'migrated',
                          json_object(
                            'id', id, 'source_id', source_id, 'tier', tier,
                            'url', url, 'guid_hash', guid_hash, 'title', title,
                            'body_excerpt', body_excerpt, 'lang', lang,
                            'published_at', published_at, 'ingested_at', ingested_at,
                            'recorded_at', COALESCE(recorded_at, ingested_at),
                            'category', category, 'theater', theater, 'country', country,
                            'lat', lat, 'lon', lon, 'numeric_payload', numeric_payload,
                            'simhash', simhash, 'cluster_id', cluster_id, 'score', score,
                            'level', level, 'matched_rules', matched_rules,
                            'origin_domain', origin_domain, 'status', status, 'kind', kind)
                   FROM events
                   WHERE id NOT IN (SELECT event_id FROM event_revisions)"""
            )

    async def _add_column(self, table: str, column: str, kind: str) -> None:
        assert self.conn is not None
        cur = await self.conn.execute(f"PRAGMA table_info({table})")
        columns = {row[1] for row in await cur.fetchall()}
        await cur.close()
        if column not in columns:
            await self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {kind}")

    async def close(self) -> None:
        if self.conn is not None:
            try:
                await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            except Exception:
                pass
            await self.conn.close()
            self.conn = None

    # ---------- primitives ----------
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Cursor:
        assert self.conn is not None
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            await self.conn.commit()
            return cur

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> aiosqlite.Row | None:
        assert self.conn is not None
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            row = await cur.fetchone()
            await cur.close()
            return row

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[aiosqlite.Row]:
        assert self.conn is not None
        async with self._lock:
            cur = await self.conn.execute(sql, tuple(params))
            rows = await cur.fetchall()
            await cur.close()
            return list(rows)

    # ---------- events ----------
    async def insert_event(self, event: Event) -> int | None:
        if event.ingested_at is None:
            event.ingested_at = utcnow()
        if event.recorded_at is None:
            event.recorded_at = utcnow()
        row = event.row()
        cols = ", ".join(row.keys())
        placeholders = ", ".join("?" for _ in row)
        cur = await self.execute(
            f"INSERT OR IGNORE INTO events ({cols}) VALUES ({placeholders})", row.values()
        )
        if cur.rowcount == 0:
            return None
        event.id = cur.lastrowid
        await self._record_event_revision(event.id, "inserted")
        return event.id

    async def _record_event_revision(self, event_id: int, reason: str) -> None:
        row = await self.fetchone("SELECT * FROM events WHERE id=?", (event_id,))
        if row is None:
            return
        snapshot = {key: row[key] for key in row.keys()}
        await self.execute(
            """INSERT INTO event_revisions (event_id, recorded_at, reason, snapshot)
               VALUES (?, ?, ?, ?)""",
            (event_id, now_ts(), reason, json_dumps(snapshot)),
        )

    async def update_event_cluster(self, event_id: int, cluster_id: int) -> None:
        await self.execute("UPDATE events SET cluster_id=? WHERE id=?", (cluster_id, event_id))
        await self._record_event_revision(event_id, "clustered")

    async def update_event_scoring(
        self, event_id: int, score: float, level: int, rules: list[str], status: str = "scored"
    ) -> None:
        await self.execute(
            "UPDATE events SET score=?, level=?, matched_rules=?, status=? WHERE id=?",
            (round(score, 3), level, json_dumps(rules), status, event_id),
        )
        await self._record_event_revision(event_id, status)

    async def update_event_payload(self, event_id: int, payload: dict[str, Any]) -> None:
        await self.execute(
            "UPDATE events SET numeric_payload=? WHERE id=?", (json_dumps(payload), event_id)
        )
        await self._record_event_revision(event_id, "payload")

    async def update_event_revision(self, event_id: int, event: "Event") -> None:
        await self.execute(
            """UPDATE events SET title=?, body_excerpt=?, url=?, numeric_payload=?,
                   simhash=?, published_at=?, status='revised' WHERE id=?""",
            (
                event.title, event.body_excerpt, event.url,
                json_dumps(event.numeric_payload), event.simhash,
                to_ts(event.published_at), event_id,
            ),
        )
        await self._record_event_revision(event_id, "revised")

    async def event_as_of(self, event_id: int, as_of: int) -> "Event | None":
        """The version of an event the system held at transaction time `as_of`."""
        row = await self.fetchone(
            """SELECT snapshot FROM event_revisions
               WHERE event_id=? AND recorded_at<=?
               ORDER BY recorded_at DESC, id DESC LIMIT 1""",
            (event_id, as_of),
        )
        return Event.from_row(json_loads(row["snapshot"])) if row else None

    async def events_as_of(self, as_of: int, since_ts: int = 0) -> list["Event"]:
        """Latest version of every event known at `as_of` with ingested_at >= since_ts."""
        rows = await self.fetchall(
            """SELECT snapshot FROM (
                   SELECT snapshot,
                          ROW_NUMBER() OVER (
                              PARTITION BY event_id ORDER BY recorded_at DESC, id DESC
                          ) AS rn,
                          json_extract(snapshot, '$.ingested_at') AS ingested
                   FROM event_revisions
                   WHERE recorded_at<=?
               )
               WHERE rn=1 AND ingested>=?
               ORDER BY ingested DESC""",
            (as_of, since_ts),
        )
        return [Event.from_row(json_loads(row["snapshot"])) for row in rows]

    async def revision_history(self, event_id: int) -> list[dict[str, Any]]:
        rows = await self.fetchall(
            """SELECT recorded_at, reason, snapshot FROM event_revisions
               WHERE event_id=? ORDER BY recorded_at, id""",
            (event_id,),
        )
        return [
            {
                "recorded_at": from_ts(row["recorded_at"]),
                "reason": row["reason"],
                "event": Event.from_row(json_loads(row["snapshot"])),
            }
            for row in rows
        ]

    async def event_by_guid_hash(self, guid_hash: str) -> "Event | None":
        row = await self.fetchone("SELECT * FROM events WHERE guid_hash=?", (guid_hash,))
        return Event.from_row(row) if row else None

    async def get_event(self, event_id: int) -> Event | None:
        row = await self.fetchone("SELECT * FROM events WHERE id=?", (event_id,))
        return Event.from_row(row) if row else None

    async def event_by_url(self, url: str) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM events WHERE url=?", (url,))

    async def recent_events(
        self,
        since_ts: int,
        theater: str | None = None,
        category: str | None = None,
        limit: int = 500,
        kinds: tuple[str, ...] = ("event",),
    ) -> list[Event]:
        sql = "SELECT * FROM events WHERE ingested_at >= ?"
        params: list[Any] = [since_ts]
        if theater:
            sql += " AND theater=?"
            params.append(theater)
        if category:
            sql += " AND category=?"
            params.append(category)
        if kinds:
            sql += " AND kind IN (%s)" % ",".join("?" for _ in kinds)
            params.extend(kinds)
        sql += " ORDER BY ingested_at DESC LIMIT ?"
        params.append(limit)
        rows = await self.fetchall(sql, params)
        return [Event.from_row(r) for r in rows]

    async def last_event_ts(self) -> int | None:
        row = await self.fetchone("SELECT MAX(ingested_at) AS ts FROM events")
        return int(row["ts"]) if row and row["ts"] else None

    async def events_for_cluster(self, cluster_id: int) -> list[Event]:
        rows = await self.fetchall(
            "SELECT * FROM events WHERE cluster_id=? ORDER BY ingested_at", (cluster_id,)
        )
        return [Event.from_row(r) for r in rows]

    # ---------- seen ----------
    async def seen_has(self, hash_value: str) -> bool:
        row = await self.fetchone("SELECT 1 FROM seen WHERE hash=?", (hash_value,))
        return row is not None

    async def seen_add(self, hash_value: str, kind: str) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO seen (hash, kind, seen_at) VALUES (?, ?, ?)",
            (hash_value, kind, now_ts()),
        )

    # ---------- clusters ----------
    async def insert_cluster(self, cluster: Cluster) -> int:
        now = now_ts()
        cur = await self.execute(
            """INSERT INTO clusters
               (theater, category, title_canonical, first_seen, last_seen, sources_count,
                tiers_bitmap, total_score, level, state, country, lat, lon, tripwire_id, meta)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                cluster.theater, cluster.category, cluster.title_canonical,
                to_ts(cluster.first_seen) or now, to_ts(cluster.last_seen) or now,
                cluster.sources_count, cluster.tiers_bitmap, cluster.total_score, cluster.level,
                cluster.state, cluster.country, cluster.lat, cluster.lon, cluster.tripwire_id,
                json_dumps(cluster.meta),
            ),
        )
        cluster.id = cur.lastrowid
        return cluster.id

    async def get_cluster(self, cluster_id: int) -> Cluster | None:
        row = await self.fetchone("SELECT * FROM clusters WHERE id=?", (cluster_id,))
        return self._cluster(row) if row else None

    @staticmethod
    def _cluster(row: aiosqlite.Row) -> Cluster:
        return Cluster(
            id=row["id"], theater=row["theater"], category=row["category"],
            title_canonical=row["title_canonical"] or "",
            first_seen=from_ts(row["first_seen"]), last_seen=from_ts(row["last_seen"]),
            sources_count=row["sources_count"] or 0, tiers_bitmap=row["tiers_bitmap"] or 0,
            total_score=row["total_score"] or 0.0,
            score_initial=(row["score_initial"] if "score_initial" in row.keys() else None),
            level=row["level"] or 0,
            tg_message_id=row["tg_message_id"], tg_thread_id=row["tg_thread_id"],
            state=row["state"] or "open", country=row["country"], lat=row["lat"], lon=row["lon"],
            last_alert_at=from_ts(row["last_alert_at"]), ack_at=from_ts(row["ack_at"]),
            tripwire_id=row["tripwire_id"], meta=json_loads(row["meta"]),
        )

    async def open_clusters(self, theater: str, category: str, since_ts: int, limit: int = 20) -> list[Cluster]:
        rows = await self.fetchall(
            """SELECT * FROM clusters
               WHERE state='open' AND theater=? AND category=? AND last_seen>=?
               ORDER BY last_seen DESC LIMIT ?""",
            (theater, category, since_ts, limit),
        )
        return [self._cluster(r) for r in rows]

    async def update_cluster(self, cluster: Cluster) -> None:
        assert cluster.id is not None
        await self.execute(
            """UPDATE clusters SET last_seen=?, sources_count=?, tiers_bitmap=?, total_score=?,
               level=?, tg_message_id=?, tg_thread_id=?, state=?, country=?, lat=?, lon=?,
               last_alert_at=?, ack_at=?, meta=?, title_canonical=?
               WHERE id=?""",
            (
                to_ts(cluster.last_seen), cluster.sources_count, cluster.tiers_bitmap,
                round(cluster.total_score, 3), cluster.level, cluster.tg_message_id,
                cluster.tg_thread_id, cluster.state, cluster.country, cluster.lat, cluster.lon,
                to_ts(cluster.last_alert_at), to_ts(cluster.ack_at), json_dumps(cluster.meta),
                cluster.title_canonical, cluster.id,
            ),
        )

    async def set_cluster_initial_score(self, cluster_id: int, score: float) -> bool:
        """Freeze the score seen on the first event; later events must not move it."""
        cur = await self.execute(
            "UPDATE clusters SET score_initial=? WHERE id=? AND score_initial IS NULL",
            (round(float(score), 3), cluster_id),
        )
        return cur.rowcount > 0

    async def add_cluster_source(self, cluster_id: int, source_id: str) -> bool:
        now = now_ts()
        cur = await self.execute(
            "INSERT OR IGNORE INTO cluster_sources (cluster_id, source_id, first_seen) VALUES (?, ?, ?)",
            (cluster_id, source_id, now),
        )
        if cur.rowcount == 0:
            return False
        row = await self.fetchone(
            "SELECT COUNT(*) AS n, MAX(first_seen) AS last FROM cluster_sources WHERE cluster_id=?",
            (cluster_id,),
        )
        if row:
            await self.execute(
                "UPDATE clusters SET sources_count=? WHERE id=?", (row["n"], cluster_id)
            )
        return True

    async def cluster_sources(self, cluster_id: int) -> list[str]:
        rows = await self.fetchall(
            "SELECT source_id FROM cluster_sources WHERE cluster_id=? ORDER BY first_seen",
            (cluster_id,),
        )
        return [r["source_id"] for r in rows]

    async def cluster_origin_rows(self, cluster_id: int) -> list[dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT source_id, origin_domain, tier FROM events WHERE cluster_id=?",
            (cluster_id,),
        )
        return [dict(r) for r in rows]

    async def open_cluster_count(self) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS n FROM clusters WHERE state='open'")
        return int(row["n"]) if row else 0

    # ---------- theater state ----------
    async def theater_get(self, theater: str) -> dict[str, Any]:
        row = await self.fetchone("SELECT * FROM theater_state WHERE theater=?", (theater,))
        if not row:
            return {"theater": theater, "score": 0.0, "level": 0, "level_since": None,
                    "last_update": None, "cooldown_until": None, "muted_until": None}
        return dict(row)

    async def theater_all(self) -> list[dict[str, Any]]:
        rows = await self.fetchall("SELECT * FROM theater_state ORDER BY theater")
        return [dict(r) for r in rows]

    async def theater_mute(self, theater: str, until_ts: int) -> None:
        await self.execute(
            """INSERT INTO theater_state (theater, muted_until) VALUES (?, ?)
               ON CONFLICT(theater) DO UPDATE SET muted_until=excluded.muted_until""",
            (theater, until_ts),
        )

    async def theater_set(
        self, theater: str, score: float, level: int, level_since: int | None,
        cooldown_until: int | None = None, muted_until: int | None = None,
    ) -> None:
        await self.execute(
            """INSERT INTO theater_state (theater, score, level, level_since, last_update, cooldown_until, muted_until)
               VALUES (?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(theater) DO UPDATE SET
                 score=excluded.score, level=excluded.level,
                 level_since=excluded.level_since, last_update=excluded.last_update,
                 cooldown_until=excluded.cooldown_until,
                 muted_until=COALESCE(excluded.muted_until, theater_state.muted_until)""",
            (theater, round(score, 3), level, level_since, now_ts(),
             cooldown_until, muted_until),
        )

    # ---------- alerts ----------
    async def insert_alert(
        self, cluster_id: int | None, level: int, tg_message_id: int | None,
        explain: dict[str, Any], tripwire_id: str | None = None, status: str = "sent",
        decided_at: int | None = None,
    ) -> int:
        now = now_ts()
        cur = await self.execute(
            """INSERT INTO alerts
               (cluster_id, level, sent_at, tg_message_id, explain, tripwire_id, status, decided_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (cluster_id, level, now, tg_message_id, json_dumps(explain), tripwire_id,
             status, decided_at or now),
        )
        return cur.lastrowid

    async def get_alert(self, alert_id: int) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM alerts WHERE id=?", (alert_id,))

    async def alert_sent(self, alert_id: int, message_id: int) -> None:
        await self.execute(
            "UPDATE alerts SET tg_message_id=?, status='sent' WHERE id=?", (message_id, alert_id)
        )

    async def alert_set_ack(self, alert_id: int) -> None:
        await self.execute("UPDATE alerts SET ack_at=? WHERE id=?", (now_ts(), alert_id))

    async def alert_set_explain(self, alert_id: int, explain: dict[str, Any]) -> None:
        await self.execute(
            "UPDATE alerts SET explain=? WHERE id=?", (json_dumps(explain), alert_id)
        )

    async def alert_update_status(self, alert_id: int, status: str,
                                 message_id: int | None = None) -> None:
        if message_id is None:
            await self.execute("UPDATE alerts SET status=? WHERE id=?", (status, alert_id))
        else:
            await self.execute(
                "UPDATE alerts SET status=?, tg_message_id=? WHERE id=?",
                (status, message_id, alert_id),
            )

    async def alerts_for_cluster(self, cluster_id: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM alerts WHERE cluster_id=? ORDER BY sent_at", (cluster_id,)
        )

    async def set_cluster_message(self, cluster_id: int, message_id: int, thread_id: int | None) -> None:
        await self.execute(
            "UPDATE clusters SET tg_message_id=?, tg_thread_id=? WHERE id=?",
            (message_id, thread_id, cluster_id),
        )

    async def cluster_mark_alert(self, cluster_id: int, ts: int) -> None:
        await self.execute(
            "UPDATE clusters SET last_alert_at=? WHERE id=?", (ts, cluster_id)
        )

    async def last_theater_alert(self, theater: str) -> int | None:
        row = await self.fetchone(
            """SELECT MAX(a.sent_at) AS ts FROM alerts a
               JOIN clusters c ON c.id = a.cluster_id
               WHERE c.theater=? AND a.level>=2 AND a.status IN ('sent', 'dry_run')""",
            (theater,),
        )
        ts = row["ts"] if row else None
        return int(ts) if ts else None

    async def latest_alert(self) -> aiosqlite.Row | None:
        return await self.fetchone("SELECT * FROM alerts ORDER BY id DESC LIMIT 1")

    async def latest_alerts(self, limit: int = 10) -> list[aiosqlite.Row]:
        return await self.fetchall("SELECT * FROM alerts ORDER BY id DESC LIMIT ?", (limit,))

    async def last_alert_time(self, cluster_id: int) -> int | None:
        row = await self.fetchone(
            "SELECT MAX(sent_at) AS ts FROM alerts WHERE cluster_id=?", (cluster_id,)
        )
        return row["ts"] if row and row["ts"] else None

    async def last_tripwire_alert(self, tripwire_id: str) -> int | None:
        row = await self.fetchone(
            "SELECT MAX(sent_at) AS ts FROM alerts WHERE tripwire_id=?", (tripwire_id,)
        )
        return row["ts"] if row and row["ts"] else None

    async def latest_alert_by_tripwire(self, tripwire_id: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM alerts WHERE tripwire_id=? ORDER BY id DESC LIMIT 1", (tripwire_id,)
        )

    async def alerts_since(self, since_ts: int, level: int | None = None) -> list[aiosqlite.Row]:
        if level is None:
            return await self.fetchall("SELECT * FROM alerts WHERE sent_at>=? ORDER BY sent_at", (since_ts,))
        return await self.fetchall(
            "SELECT * FROM alerts WHERE sent_at>=? AND level=? ORDER BY sent_at", (since_ts, level)
        )

    async def unanswered_critical(self, older_than_ts: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            """SELECT a.* FROM alerts a
               WHERE a.level=3 AND a.ack_at IS NULL AND a.sent_at<=?
                 AND a.id=(SELECT MAX(id) FROM alerts WHERE cluster_id=a.cluster_id)
               ORDER BY a.sent_at""",
            (older_than_ts,),
        )

    async def ack_alert(self, alert_id: int) -> bool:
        row = await self.fetchone("SELECT * FROM alerts WHERE id=?", (alert_id,))
        if not row:
            return False
        now = now_ts()
        await self.execute("UPDATE alerts SET ack_at=? WHERE id=?", (now, alert_id))
        if row["cluster_id"]:
            await self.execute(
                "UPDATE clusters SET ack_at=? WHERE id=?", (now, row["cluster_id"])
            )
        return True

    async def ack_latest(self, cluster_id: int | None = None) -> int:
        if cluster_id is None:
            row = await self.fetchone("SELECT id FROM alerts WHERE ack_at IS NULL ORDER BY id DESC LIMIT 1")
        else:
            row = await self.fetchone(
                "SELECT id FROM alerts WHERE cluster_id=? AND ack_at IS NULL ORDER BY id DESC LIMIT 1",
                (cluster_id,),
            )
        if not row:
            return 0
        now = now_ts()
        await self.execute("UPDATE alerts SET ack_at=? WHERE id=?", (now, row["id"]))
        if cluster_id is not None:
            await self.execute("UPDATE clusters SET ack_at=? WHERE id=?", (now, cluster_id))
        return int(row["id"])

    # ---------- config journal ----------
    async def journal_add(self, actor: str, action: str, payload: dict[str, Any]) -> int:
        cur = await self.execute(
            "INSERT INTO config_journal (ts, actor, action, payload) VALUES (?, ?, ?, ?)",
            (now_ts(), actor, action, json_dumps(payload)),
        )
        return cur.lastrowid

    async def journal_recent(self, limit: int = 10) -> list[dict[str, Any]]:
        rows = await self.fetchall(
            "SELECT * FROM config_journal ORDER BY id DESC LIMIT ?", (limit,)
        )
        return [
            {"id": r["id"], "ts": r["ts"], "actor": r["actor"], "action": r["action"],
             "payload": json_loads(r["payload"], {})}
            for r in rows
        ]

    # ---------- source health ----------
    async def health_all(self) -> list[dict[str, Any]]:
        rows = await self.fetchall("SELECT * FROM source_health ORDER BY source_id")
        return [dict(r) for r in rows]

    async def health_get(self, source_id: str) -> dict[str, Any]:
        row = await self.fetchone("SELECT * FROM source_health WHERE source_id=?", (source_id,))
        return dict(row) if row else {}

    async def health_ok(
        self, source_id: str, latency_ms: float, items: int, etag: str | None,
        last_modified: str | None, not_modified: bool = False,
    ) -> None:
        now = now_ts()
        row = await self.fetchone("SELECT * FROM source_health WHERE source_id=?", (source_id,))
        prev_avg = row["avg_latency_ms"] if row and row["avg_latency_ms"] else 0.0
        avg = (prev_avg * 0.8 + latency_ms * 0.2) if prev_avg else latency_ms
        await self.execute(
            """INSERT INTO source_health
               (source_id, last_ok, last_checked, consecutive_failures, avg_latency_ms,
                etag, last_modified, last_items, first_error_at, total_fetches, total_items)
               VALUES (?, ?, ?, 0, ?, ?, ?, ?, 0, 1, ?)
               ON CONFLICT(source_id) DO UPDATE SET
                 last_ok=excluded.last_ok, last_checked=excluded.last_checked,
                 consecutive_failures=0, avg_latency_ms=excluded.avg_latency_ms,
                 etag=COALESCE(excluded.etag, source_health.etag),
                 last_modified=COALESCE(excluded.last_modified, source_health.last_modified),
                 last_items=excluded.last_items, first_error_at=0,
                 total_fetches=source_health.total_fetches+1,
                 total_items=source_health.total_items+excluded.total_items""",
            (source_id, now, now, avg, etag, last_modified, items, items),
        )

    async def health_error(self, source_id: str, error: str) -> None:
        now = now_ts()
        await self.execute(
            """INSERT INTO source_health
               (source_id, last_checked, last_error, last_error_at, consecutive_failures,
                first_error_at, total_fetches, total_errors)
               VALUES (?, ?, ?, ?, 1, ?, 1, 1)
               ON CONFLICT(source_id) DO UPDATE SET
                 last_checked=excluded.last_checked, last_error=excluded.last_error,
                 last_error_at=excluded.last_error_at,
                 consecutive_failures=source_health.consecutive_failures+1,
                 first_error_at=CASE WHEN source_health.consecutive_failures=0
                                     THEN excluded.first_error_at
                                     ELSE COALESCE(source_health.first_error_at,
                                                   excluded.first_error_at) END,
                 total_fetches=source_health.total_fetches+1,
                 total_errors=source_health.total_errors+1""",
            (source_id, now, error[:500], now, now),
        )

    # ---------- meta / indicators / markets ----------
    async def meta_set(self, key: str, value: Any) -> None:
        await self.execute(
            "INSERT OR REPLACE INTO meta (key, value, updated_at) VALUES (?, ?, ?)",
            (key, json_dumps(value) if not isinstance(value, str) else value, now_ts()),
        )

    async def meta_get(self, key: str, default: Any = None) -> Any:
        row = await self.fetchone("SELECT value FROM meta WHERE key=?", (key,))
        if not row:
            return default
        raw = row["value"]
        try:
            value = _json.loads(raw)
        except (TypeError, ValueError):
            return raw
        return value

    async def indicator_set(self, name: str, value: str, meta: dict[str, Any] | None = None) -> None:
        await self.execute(
            """INSERT OR REPLACE INTO indicator_state (name, value, updated_at, meta)
               VALUES (?, ?, ?, ?)""",
            (name, value, now_ts(), json_dumps(meta or {})),
        )

    async def indicator_get(self, name: str) -> dict[str, Any] | None:
        row = await self.fetchone("SELECT * FROM indicator_state WHERE name=?", (name,))
        if not row:
            return None
        return {"name": row["name"], "value": row["value"], "updated_at": row["updated_at"],
                "meta": json_loads(row["meta"])}

    async def indicator_all(self) -> list[dict[str, Any]]:
        rows = await self.fetchall("SELECT * FROM indicator_state ORDER BY name")
        return [{"name": r["name"], "value": r["value"], "updated_at": r["updated_at"],
                 "meta": json_loads(r["meta"])} for r in rows]

    async def market_add(self, symbol: str, ts: int, close: float | None,
                         change_pct: float | None, source_id: str) -> None:
        await self.execute(
            "INSERT INTO market_samples (symbol, ts, close, change_pct, source_id) VALUES (?, ?, ?, ?, ?)",
            (symbol, ts, close, change_pct, source_id),
        )

    async def market_recent(self, symbol: str, since_ts: int) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM market_samples WHERE symbol=? AND ts>=? ORDER BY ts", (symbol, since_ts)
        )

    async def market_last(self, symbol: str) -> aiosqlite.Row | None:
        return await self.fetchone(
            "SELECT * FROM market_samples WHERE symbol=? ORDER BY ts DESC LIMIT 1", (symbol,)
        )

    # ---------- heartbeat / pending ----------
    async def heartbeat(self, component: str, detail: str = "") -> None:
        await self.execute(
            "INSERT INTO heartbeats (ts, component, detail) VALUES (?, ?, ?)",
            (now_ts(), component, detail[:300]),
        )

    async def last_heartbeat(self, component: str = "main") -> int | None:
        row = await self.fetchone(
            "SELECT MAX(ts) AS ts FROM heartbeats WHERE component=?", (component,)
        )
        return row["ts"] if row and row["ts"] else None

    async def pending_add(self, due_at: int, kind: str, payload: dict[str, Any]) -> int:
        cur = await self.execute(
            "INSERT INTO pending_actions (created_at, due_at, kind, payload) VALUES (?, ?, ?, ?)",
            (now_ts(), due_at, kind, json_dumps(payload)),
        )
        return cur.lastrowid

    async def pending_due(self, now_ts: int, limit: int = 50) -> list[aiosqlite.Row]:
        return await self.fetchall(
            "SELECT * FROM pending_actions WHERE status='pending' AND due_at<=? ORDER BY due_at LIMIT ?",
            (now_ts, limit),
        )

    async def pending_done(self, action_id: int, status: str = "done") -> None:
        await self.execute("UPDATE pending_actions SET status=? WHERE id=?", (status, action_id))

    # ---------- translations ----------
    async def translation_get(self, hash_value: str) -> str | None:
        row = await self.fetchone("SELECT target FROM translations WHERE hash=?", (hash_value,))
        return row["target"] if row else None

    async def translation_put(
        self, hash_value: str, provider: str, dst_lang: str, source: str, target: str,
    ) -> None:
        await self.execute(
            """INSERT OR REPLACE INTO translations
               (hash, provider, dst_lang, source, target, created_at) VALUES (?, ?, ?, ?, ?, ?)""",
            (hash_value, provider, dst_lang, source, target, now_ts()),
        )

    async def translation_count(self) -> int:
        row = await self.fetchone("SELECT COUNT(*) AS n FROM translations")
        return int(row["n"]) if row else 0

    # ---------- maintenance ----------
    async def cleanup(
        self, event_days: int = 180, seen_days: int = 14, translation_days: int = 180,
    ) -> None:
        now = now_ts()
        await self.execute("DELETE FROM events WHERE ingested_at < ?", (now - event_days * 86400,))
        await self.execute(
            "DELETE FROM event_revisions WHERE event_id NOT IN (SELECT id FROM events)"
        )
        await self.execute("DELETE FROM seen WHERE seen_at < ?", (now - seen_days * 86400,))
        await self.execute(
            "DELETE FROM translations WHERE created_at < ?", (now - translation_days * 86400,)
        )
        await self.execute("DELETE FROM heartbeats WHERE ts < ?", (now - 30 * 86400,))
        await self.execute("DELETE FROM market_samples WHERE ts < ?", (now - 90 * 86400,))
        await self.execute(
            "UPDATE clusters SET state='closed' WHERE state='open' AND last_seen < ?",
            (now - 7 * 86400,),
        )

    async def vacuum(self) -> None:
        assert self.conn is not None
        async with self._lock:
            # PRAGMA returns a row; an unread cursor keeps the statement active
            # and makes SQLite reject VACUUM with "SQL statements in progress".
            cur = await self.conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            await cur.fetchall()
            await cur.close()
            cur = await self.conn.execute("VACUUM")
            await cur.close()
            await self.conn.commit()

    async def backup(self, target: Path) -> None:
        target.parent.mkdir(parents=True, exist_ok=True)
        if target.exists():
            target.unlink()
        await self.execute("VACUUM INTO ?", (str(target),))

    async def size_bytes(self) -> int:
        total = 0
        for suffix in ("", "-wal", "-shm"):
            p = Path(str(self.path) + suffix)
            if p.exists():
                total += p.stat().st_size
        return total
