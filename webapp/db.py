"""Thin Postgres layer: a small connection pool and dict-row helpers.

Works with Supabase's connection pooler in session or transaction mode (prepared statements
are disabled so transaction mode is safe).
"""
from __future__ import annotations

import threading
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row
from psycopg.types.json import Jsonb
from psycopg_pool import ConnectionPool

from .config import settings

_pool: ConnectionPool | None = None
_lock = threading.Lock()


def pool() -> ConnectionPool:
    global _pool
    if _pool is None:
        with _lock:
            if _pool is None:
                _pool = ConnectionPool(
                    conninfo=settings.database_url, min_size=0, max_size=6, open=True, timeout=20,
                    kwargs={"row_factory": dict_row, "prepare_threshold": None, "autocommit": True},
                    check=ConnectionPool.check_connection, max_idle=120)
    return _pool


def close():
    global _pool
    if _pool is not None:
        _pool.close()
        _pool = None


@contextmanager
def conn():
    with pool().connection() as c:
        yield c


@contextmanager
def tx():
    """A transaction: everything inside commits together or not at all."""
    with pool().connection() as c:
        with c.transaction():
            yield c


def one(sql: str, params=None, c=None) -> dict | None:
    if c is not None:
        return c.execute(sql, params).fetchone()
    with conn() as c2:
        return c2.execute(sql, params).fetchone()


def all(sql: str, params=None, c=None) -> list[dict]:  # noqa: A001 - mirrors the SQL word
    if c is not None:
        return c.execute(sql, params).fetchall()
    with conn() as c2:
        return c2.execute(sql, params).fetchall()


def run(sql: str, params=None, c=None) -> int:
    if c is not None:
        return c.execute(sql, params).rowcount
    with conn() as c2:
        return c2.execute(sql, params).rowcount


def scalar(sql: str, params=None, c=None):
    row = one(sql, params, c)
    return None if row is None else next(iter(row.values()))


def json(value) -> Jsonb:
    return Jsonb(value)


def ping() -> bool:
    try:
        return scalar("select 1") == 1
    except psycopg.Error:
        return False


def rate_ok(kind: str, key: str, limit: int, window_s: int) -> bool:
    """Record an event and say whether the caller is still under `limit` events per window."""
    n = scalar("select count(*) from pp.rate_events where kind = %s and key = %s and at > now() - make_interval(secs => %s)",
               (kind, key, window_s))
    if n >= limit:
        return False
    run("insert into pp.rate_events (kind, key) values (%s, %s)", (kind, key))
    return True
