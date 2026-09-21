import psycopg
import pytest

pytestmark = pytest.mark.integration


async def test_schema_fixture_creates_a_schema(test_dsn: str, schema: str) -> None:
    async with await psycopg.AsyncConnection.connect(test_dsn, autocommit=True) as connection:
        cursor = await connection.execute(
            "SELECT count(*) FROM information_schema.schemata WHERE schema_name = %s", (schema,)
        )
        row = await cursor.fetchone()
    assert row is not None
    assert row[0] == 1
