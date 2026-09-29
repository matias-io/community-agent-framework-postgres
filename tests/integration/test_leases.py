import asyncio
import logging
from datetime import timedelta

import pytest

from agent_framework_community_postgres import PostgresPersistence
from agent_framework_community_postgres._client import (
    ClientHandle,
    LeaseLost,
    LeaseUnavailable,
    PostgresStorageError,
    TableNames,
)
from agent_framework_community_postgres._leases import PostgresLeases

pytestmark = pytest.mark.integration


@pytest.fixture
def leases(client: ClientHandle, migrated: TableNames) -> PostgresLeases:
    return PostgresLeases(application_id="tests", client=client.client, schema=migrated.schema)


async def test_acquire_renew_release(leases: PostgresLeases) -> None:
    async with leases.acquire("threads/a", owner="one", ttl=timedelta(seconds=30)) as lease:
        assert lease.owner == "one"
        await lease.renew()
    assert await leases.try_acquire("threads/a", owner="two", ttl=timedelta(seconds=30)) is not None


async def test_second_owner_is_refused_without_waiting(leases: PostgresLeases) -> None:
    async with leases.acquire("threads/b", owner="one", ttl=timedelta(seconds=30)):
        with pytest.raises(LeaseUnavailable):
            async with leases.acquire("threads/b", owner="two", ttl=timedelta(seconds=30), wait=timedelta(0)):
                pass


async def test_same_owner_cannot_acquire_a_held_lease_again(leases: PostgresLeases) -> None:
    async with leases.acquire("threads/c", owner="one", ttl=timedelta(seconds=30)):
        assert await leases.try_acquire("threads/c", owner="one", ttl=timedelta(seconds=30)) is None
        with pytest.raises(LeaseUnavailable):
            async with leases.acquire("threads/c", owner="one", ttl=timedelta(seconds=30), wait=timedelta(0)):
                pass


async def test_one_holder_per_acquisition_even_with_one_owner(leases: PostgresLeases) -> None:
    ttl = timedelta(seconds=30)
    a = await leases.try_acquire("threads/f", owner="replica-1", ttl=ttl)
    assert a is not None
    assert await leases.try_acquire("threads/f", owner="replica-1", ttl=ttl) is None  # B, same owner, is refused
    await a.release()
    b = await leases.try_acquire("threads/f", owner="replica-1", ttl=ttl)
    assert b is not None and b.token != a.token
    assert await leases.try_acquire("threads/f", owner="replica-2", ttl=ttl) is None
    await a.release()  # A's stale release must not free B's lease
    assert await leases.try_acquire("threads/f", owner="replica-2", ttl=ttl) is None
    await b.renew()
    with pytest.raises(LeaseLost):
        await a.renew()
    await b.release()


async def test_a_crashed_holder_is_taken_over_after_expiry(leases: PostgresLeases) -> None:
    crashed = await leases.try_acquire("threads/g", owner="one", ttl=timedelta(seconds=1))
    assert crashed is not None
    assert await leases.try_acquire("threads/g", owner="one", ttl=timedelta(seconds=30)) is None
    await asyncio.sleep(1.5)
    recovered = await leases.try_acquire("threads/g", owner="one", ttl=timedelta(seconds=30))
    assert recovered is not None
    await recovered.renew()
    await recovered.release()


async def test_expired_lease_is_taken_over_and_old_holder_learns_on_renew(leases: PostgresLeases) -> None:
    first = await leases.try_acquire("threads/d", owner="one", ttl=timedelta(seconds=1))
    assert first is not None
    await asyncio.sleep(1.5)
    async with leases.acquire("threads/d", owner="two", ttl=timedelta(seconds=30), wait=timedelta(seconds=2)):
        with pytest.raises(LeaseLost):
            await first.renew()
        await first.release()  # releasing a lease you no longer own is a no-op, not an error


async def test_waiting_acquire_succeeds_once_released(leases: PostgresLeases) -> None:
    holder = await leases.try_acquire("threads/e", owner="one", ttl=timedelta(seconds=30))
    assert holder is not None

    async def release_soon() -> None:
        await asyncio.sleep(0.3)
        await holder.release()

    task = asyncio.create_task(release_soon())
    async with leases.acquire("threads/e", owner="two", ttl=timedelta(seconds=30), wait=timedelta(seconds=5)) as lease:
        assert lease.owner == "two"
    await task


async def test_body_error_survives_a_failed_release(
    persistence: PostgresPersistence, caplog: pytest.LogCaptureFixture
) -> None:
    leases = persistence.leases()
    with caplog.at_level(logging.WARNING), pytest.raises(RuntimeError, match="body failed"):
        async with leases.acquire("threads/secret-resource", owner="one", ttl=timedelta(seconds=30)):
            await persistence.close()
            raise RuntimeError("body failed")
    assert any("release" in record.getMessage() for record in caplog.records)
    assert all("secret-resource" not in record.getMessage() for record in caplog.records)


async def test_release_failure_surfaces_when_the_body_succeeded(persistence: PostgresPersistence) -> None:
    leases = persistence.leases()
    with pytest.raises(PostgresStorageError):
        async with leases.acquire("threads/h", owner="one", ttl=timedelta(seconds=30)):
            await persistence.close()


async def test_expired_lease_nobody_took_raises_lease_lost_on_renew(leases: PostgresLeases) -> None:
    lease = await leases.try_acquire("threads/i", owner="one", ttl=timedelta(seconds=1))
    assert lease is not None
    await asyncio.sleep(1.5)
    with pytest.raises(LeaseLost):
        await lease.renew()


async def test_lease_is_released_when_the_body_raises(leases: PostgresLeases) -> None:
    with pytest.raises(RuntimeError):
        async with leases.acquire("threads/j", owner="one", ttl=timedelta(seconds=30)):
            raise RuntimeError("body failed")
    other = await leases.try_acquire("threads/j", owner="two", ttl=timedelta(seconds=30))
    assert other is not None
