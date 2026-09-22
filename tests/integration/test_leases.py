import asyncio
from datetime import timedelta

import pytest

from agent_framework_community_postgres._client import LeaseLost, LeaseUnavailable, TableNames, _Client
from agent_framework_community_postgres._leases import PostgresLeases

pytestmark = pytest.mark.integration


@pytest.fixture
def leases(client: _Client, migrated: TableNames) -> PostgresLeases:
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


async def test_same_owner_can_reacquire(leases: PostgresLeases) -> None:
    async with leases.acquire("threads/c", owner="one", ttl=timedelta(seconds=30)):
        assert await leases.try_acquire("threads/c", owner="one", ttl=timedelta(seconds=30)) is not None


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
