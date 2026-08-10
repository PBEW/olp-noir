"""ชั้นเก็บข้อมูล (SQLite) สำหรับบิล, ตั๋วสอบถาม, รีวิว และ VIP"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable

import aiosqlite

SCHEMA = """
CREATE TABLE IF NOT EXISTS jobs (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id          INTEGER NOT NULL,
    job_type          TEXT    NOT NULL DEFAULT 'NORMAL',   -- NORMAL | EXTEND
    parent_job_id     INTEGER,
    customer_id       INTEGER NOT NULL,
    staff_id          INTEGER NOT NULL,
    services          TEXT    NOT NULL,                    -- JSON list ของ service key
    room              TEXT,
    note              TEXT,
    start_time        TEXT    NOT NULL,                    -- ISO UTC
    end_time          TEXT    NOT NULL,                    -- ISO UTC
    duration_minutes  INTEGER NOT NULL DEFAULT 0,
    vip_tier          TEXT,                                  -- NULL | lace | desire | obsession (ระดับตอนเปิดบิล)
    quota_services    TEXT    NOT NULL DEFAULT '[]',          -- JSON list ของ service key ที่ใช้สิทธิ์ฟรีในบิลนี้
    quota_cycle       TEXT,                                   -- รอบเดือนที่ใช้สิทธิ์ฟรี (เช่น '2026-08') ไว้คืนสิทธิ์ตอนยกเลิก
    total_price       REAL    NOT NULL DEFAULT 0,
    staff_share       REAL    NOT NULL DEFAULT 0,
    shop_share        REAL    NOT NULL DEFAULT 0,
    status            TEXT    NOT NULL DEFAULT 'PENDING_STAFF',
    opened_by         INTEGER,
    created_at        TEXT    NOT NULL,
    accepted_at       TEXT,
    paid_at           TEXT,
    cancelled_at      TEXT,
    notified_start    INTEGER NOT NULL DEFAULT 0,
    notified_end      INTEGER NOT NULL DEFAULT 0,
    review_sent       INTEGER NOT NULL DEFAULT 0,
    reviewed          INTEGER NOT NULL DEFAULT 0,
    sheet_logged      INTEGER NOT NULL DEFAULT 0,
    slip_url          TEXT
);

CREATE TABLE IF NOT EXISTS tickets (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    customer_id   INTEGER NOT NULL,
    admin_id      INTEGER,
    status        TEXT    NOT NULL DEFAULT 'OPEN',   -- OPEN | ACTIVE | CLOSED
    created_at    TEXT    NOT NULL,
    accepted_at   TEXT,
    closed_at     TEXT,
    last_activity TEXT    NOT NULL,
    admin_msg_id  INTEGER
);

CREATE TABLE IF NOT EXISTS reviews (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    job_id        INTEGER NOT NULL,
    guild_id      INTEGER NOT NULL,
    customer_id   INTEGER NOT NULL,
    staff_id      INTEGER NOT NULL,
    staff_name    TEXT,
    services      TEXT,
    stars         INTEGER NOT NULL,
    spice         INTEGER NOT NULL,
    content       TEXT,
    status        TEXT    NOT NULL DEFAULT 'PENDING', -- PENDING | APPROVED | REJECTED
    created_at    TEXT    NOT NULL,
    handled_at    TEXT,
    handled_by    INTEGER,
    admin_msg_id  INTEGER,
    public_msg_id INTEGER
);

CREATE TABLE IF NOT EXISTS vip_orders (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id        INTEGER NOT NULL,
    customer_id     INTEGER NOT NULL,
    package_key     TEXT    NOT NULL,
    base_price      REAL    NOT NULL,
    discount_code   TEXT,
    discount_amount REAL    NOT NULL DEFAULT 0,
    total_price     REAL    NOT NULL,
    status          TEXT    NOT NULL DEFAULT 'AWAITING_PAYMENT',
    created_at      TEXT    NOT NULL,
    paid_at         TEXT,
    expires_at      TEXT,
    slip_url        TEXT
);

CREATE TABLE IF NOT EXISTS vip_members (
    user_id       INTEGER PRIMARY KEY,
    tier          TEXT,             -- lace | desire | obsession
    package_key   TEXT,             -- แพ็กเกจที่ซื้อล่าสุด (NULL ถ้าได้จากการอัปเกรดฟรี)
    role_id       INTEGER,
    streak_months INTEGER NOT NULL DEFAULT 0,  -- เดือนสะสมต่อเนื่องของระดับปัจจุบัน (ใช้แจ้งอัปเกรดฟรีครบ 12 เดือน)
    expires_at    TEXT,
    updated_at    TEXT
);

CREATE TABLE IF NOT EXISTS vip_quota_usage (
    user_id     INTEGER NOT NULL,
    service_key TEXT    NOT NULL,
    cycle_month TEXT    NOT NULL,   -- 'YYYY-MM' เวลาไทย
    used        INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (user_id, service_key, cycle_month)
);

CREATE TABLE IF NOT EXISTS vip_upgrade_requests (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    guild_id      INTEGER NOT NULL,
    user_id       INTEGER NOT NULL,
    from_tier     TEXT    NOT NULL,
    to_tier       TEXT    NOT NULL,
    streak_months INTEGER NOT NULL,
    status        TEXT    NOT NULL DEFAULT 'PENDING',  -- PENDING | APPROVED | REJECTED
    created_at    TEXT    NOT NULL,
    handled_at    TEXT,
    handled_by    INTEGER,
    admin_msg_id  INTEGER
);

CREATE TABLE IF NOT EXISTS pending_slips (
    user_id    INTEGER PRIMARY KEY,
    kind       TEXT    NOT NULL,   -- JOB | VIP
    ref_id     INTEGER NOT NULL,
    created_at TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS meta (
    key   TEXT PRIMARY KEY,
    value TEXT
);

CREATE INDEX IF NOT EXISTS idx_jobs_status ON jobs(status);
CREATE INDEX IF NOT EXISTS idx_jobs_customer ON jobs(customer_id);
CREATE INDEX IF NOT EXISTS idx_tickets_customer ON tickets(customer_id, status);
"""


class Database:
    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.conn: aiosqlite.Connection | None = None

    async def connect(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.conn = await aiosqlite.connect(self.path)
        self.conn.row_factory = aiosqlite.Row
        await self.conn.execute("PRAGMA journal_mode=WAL")
        await self.conn.execute("PRAGMA foreign_keys=ON")
        await self.conn.executescript(SCHEMA)
        await self.conn.commit()

    async def close(self) -> None:
        if self.conn is not None:
            await self.conn.close()
            self.conn = None

    # ----------------------------------------------------------- low level
    async def execute(self, sql: str, params: Iterable[Any] = ()) -> int:
        assert self.conn is not None
        cur = await self.conn.execute(sql, tuple(params))
        await self.conn.commit()
        return cur.lastrowid or 0

    async def fetchone(self, sql: str, params: Iterable[Any] = ()) -> dict | None:
        assert self.conn is not None
        async with self.conn.execute(sql, tuple(params)) as cur:
            row = await cur.fetchone()
        return dict(row) if row else None

    async def fetchall(self, sql: str, params: Iterable[Any] = ()) -> list[dict]:
        assert self.conn is not None
        async with self.conn.execute(sql, tuple(params)) as cur:
            rows = await cur.fetchall()
        return [dict(r) for r in rows]

    async def _update(self, table: str, row_id: int, fields: dict[str, Any]) -> None:
        if not fields:
            return
        assign = ", ".join(f"{k} = ?" for k in fields)
        await self.execute(f"UPDATE {table} SET {assign} WHERE id = ?", (*fields.values(), row_id))

    # ---------------------------------------------------------------- jobs
    async def create_job(self, **fields: Any) -> int:
        fields = dict(fields)
        fields["services"] = json.dumps(fields.get("services", []), ensure_ascii=False)
        fields["quota_services"] = json.dumps(fields.get("quota_services", []), ensure_ascii=False)
        cols = ", ".join(fields)
        holders = ", ".join("?" for _ in fields)
        return await self.execute(f"INSERT INTO jobs ({cols}) VALUES ({holders})", tuple(fields.values()))

    @staticmethod
    def _decode_job(job: dict) -> dict:
        job["services"] = json.loads(job["services"])
        job["quota_services"] = json.loads(job["quota_services"] or "[]")
        return job

    async def get_job(self, job_id: int) -> dict | None:
        job = await self.fetchone("SELECT * FROM jobs WHERE id = ?", (job_id,))
        return self._decode_job(job) if job else None

    async def update_job(self, job_id: int, **fields: Any) -> None:
        await self._update("jobs", job_id, fields)

    async def jobs_by_status(self, statuses: Iterable[str]) -> list[dict]:
        statuses = list(statuses)
        holders = ", ".join("?" for _ in statuses)
        rows = await self.fetchall(
            f"SELECT * FROM jobs WHERE status IN ({holders}) ORDER BY start_time", statuses
        )
        return [self._decode_job(row) for row in rows]

    async def active_jobs(self) -> list[dict]:
        return await self.jobs_by_status(["PENDING_STAFF", "ACCEPTED", "SLIP_PENDING", "PAID"])

    async def jobs_paid_between(self, start_iso: str, end_iso: str) -> list[dict]:
        rows = await self.fetchall(
            "SELECT * FROM jobs WHERE status IN ('PAID','COMPLETED') "
            "AND paid_at >= ? AND paid_at < ? ORDER BY paid_at",
            (start_iso, end_iso),
        )
        return [self._decode_job(row) for row in rows]

    # ------------------------------------------------------------- tickets
    async def create_ticket(self, **fields: Any) -> int:
        cols = ", ".join(fields)
        holders = ", ".join("?" for _ in fields)
        return await self.execute(
            f"INSERT INTO tickets ({cols}) VALUES ({holders})", tuple(fields.values())
        )

    async def get_ticket(self, ticket_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM tickets WHERE id = ?", (ticket_id,))

    async def update_ticket(self, ticket_id: int, **fields: Any) -> None:
        await self._update("tickets", ticket_id, fields)

    async def open_ticket_for_customer(self, customer_id: int) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM tickets WHERE customer_id = ? AND status IN ('OPEN','ACTIVE') "
            "ORDER BY id DESC LIMIT 1",
            (customer_id,),
        )

    async def active_ticket_for_admin(self, admin_id: int) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM tickets WHERE admin_id = ? AND status = 'ACTIVE' ORDER BY id DESC LIMIT 1",
            (admin_id,),
        )

    async def tickets_by_status(self, statuses: Iterable[str]) -> list[dict]:
        statuses = list(statuses)
        holders = ", ".join("?" for _ in statuses)
        return await self.fetchall(f"SELECT * FROM tickets WHERE status IN ({holders})", statuses)

    # ------------------------------------------------------------- reviews
    async def create_review(self, **fields: Any) -> int:
        cols = ", ".join(fields)
        holders = ", ".join("?" for _ in fields)
        return await self.execute(
            f"INSERT INTO reviews ({cols}) VALUES ({holders})", tuple(fields.values())
        )

    async def get_review(self, review_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM reviews WHERE id = ?", (review_id,))

    async def update_review(self, review_id: int, **fields: Any) -> None:
        await self._update("reviews", review_id, fields)

    async def review_for_job(self, job_id: int) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM reviews WHERE job_id = ? AND status != 'REJECTED' ORDER BY id DESC LIMIT 1",
            (job_id,),
        )

    # ----------------------------------------------------------- vip orders
    async def create_vip_order(self, **fields: Any) -> int:
        cols = ", ".join(fields)
        holders = ", ".join("?" for _ in fields)
        return await self.execute(
            f"INSERT INTO vip_orders ({cols}) VALUES ({holders})", tuple(fields.values())
        )

    async def get_vip_order(self, order_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM vip_orders WHERE id = ?", (order_id,))

    async def update_vip_order(self, order_id: int, **fields: Any) -> None:
        await self._update("vip_orders", order_id, fields)

    async def upsert_vip_member(
        self,
        user_id: int,
        tier: str,
        package_key: str | None,
        role_id: int,
        streak_months: int,
        expires_at: str,
        updated_at: str,
    ) -> None:
        await self.execute(
            "INSERT INTO vip_members (user_id, tier, package_key, role_id, streak_months, expires_at, updated_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?) ON CONFLICT(user_id) DO UPDATE SET "
            "tier = excluded.tier, package_key = excluded.package_key, role_id = excluded.role_id, "
            "streak_months = excluded.streak_months, expires_at = excluded.expires_at, "
            "updated_at = excluded.updated_at",
            (user_id, tier, package_key, role_id, streak_months, expires_at, updated_at),
        )

    async def get_vip_member(self, user_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM vip_members WHERE user_id = ?", (user_id,))

    async def active_vip_member(self, user_id: int, now_iso: str) -> dict | None:
        """คืนสถานะ VIP เฉพาะที่ยังไม่หมดอายุ (None ถ้าหมดอายุ/ไม่เคยเป็น VIP)"""
        row = await self.get_vip_member(user_id)
        if row and row["expires_at"] and row["expires_at"] > now_iso:
            return row
        return None

    # ------------------------------------------------------------- quota VIP
    async def get_quota_used(self, user_id: int, service_key: str, cycle_month: str) -> int:
        row = await self.fetchone(
            "SELECT used FROM vip_quota_usage WHERE user_id = ? AND service_key = ? AND cycle_month = ?",
            (user_id, service_key, cycle_month),
        )
        return int(row["used"]) if row else 0

    async def reserve_quota(self, user_id: int, service_key: str, cycle_month: str) -> None:
        await self.execute(
            "INSERT INTO vip_quota_usage (user_id, service_key, cycle_month, used) VALUES (?, ?, ?, 1) "
            "ON CONFLICT(user_id, service_key, cycle_month) DO UPDATE SET used = used + 1",
            (user_id, service_key, cycle_month),
        )

    async def release_quota(self, user_id: int, service_key: str, cycle_month: str) -> None:
        await self.execute(
            "UPDATE vip_quota_usage SET used = MAX(used - 1, 0) "
            "WHERE user_id = ? AND service_key = ? AND cycle_month = ?",
            (user_id, service_key, cycle_month),
        )

    # ------------------------------------------------------ upgrade requests
    async def create_upgrade_request(self, **fields: Any) -> int:
        cols = ", ".join(fields)
        holders = ", ".join("?" for _ in fields)
        return await self.execute(
            f"INSERT INTO vip_upgrade_requests ({cols}) VALUES ({holders})", tuple(fields.values())
        )

    async def get_upgrade_request(self, request_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM vip_upgrade_requests WHERE id = ?", (request_id,))

    async def update_upgrade_request(self, request_id: int, **fields: Any) -> None:
        await self._update("vip_upgrade_requests", request_id, fields)

    async def pending_upgrade_request(self, user_id: int, to_tier: str) -> dict | None:
        return await self.fetchone(
            "SELECT * FROM vip_upgrade_requests WHERE user_id = ? AND to_tier = ? AND status = 'PENDING'",
            (user_id, to_tier),
        )

    # ------------------------------------------------------- pending slips
    async def set_pending_slip(self, user_id: int, kind: str, ref_id: int, created_at: str) -> None:
        await self.execute(
            "INSERT INTO pending_slips (user_id, kind, ref_id, created_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(user_id) DO UPDATE SET kind = excluded.kind, ref_id = excluded.ref_id, "
            "created_at = excluded.created_at",
            (user_id, kind, ref_id, created_at),
        )

    async def get_pending_slip(self, user_id: int) -> dict | None:
        return await self.fetchone("SELECT * FROM pending_slips WHERE user_id = ?", (user_id,))

    async def clear_pending_slip(self, user_id: int) -> None:
        await self.execute("DELETE FROM pending_slips WHERE user_id = ?", (user_id,))

    # ---------------------------------------------------------------- meta
    async def get_meta(self, key: str, default: str | None = None) -> str | None:
        row = await self.fetchone("SELECT value FROM meta WHERE key = ?", (key,))
        return row["value"] if row else default

    async def set_meta(self, key: str, value: str) -> None:
        await self.execute(
            "INSERT INTO meta (key, value) VALUES (?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )
