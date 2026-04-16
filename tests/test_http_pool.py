"""Shared httpx.AsyncClient singleton."""
from __future__ import annotations

import pytest

from src.http_pool import SharedHttpPool


async def test_singleton_returns_same_client():
    pool = SharedHttpPool()
    c1 = await pool.client()
    c2 = await pool.client()
    assert c1 is c2
    await pool.close()


async def test_closed_pool_rebuilds_on_next_call():
    pool = SharedHttpPool()
    c1 = await pool.client()
    await pool.close()
    c2 = await pool.client()
    assert c1 is not c2
    await pool.close()


async def test_close_is_idempotent():
    pool = SharedHttpPool()
    await pool.close()   # no-op on fresh pool
    await pool.client()
    await pool.close()
    await pool.close()   # also no-op
