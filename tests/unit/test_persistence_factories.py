from datetime import timedelta

from agent_framework_community_postgres import PostgresPersistence, RetentionPolicy

DAY = RetentionPolicy(ttl=timedelta(days=1))
HOUR = RetentionPolicy(ttl=timedelta(hours=1))


async def test_factories_default_to_the_hub_policy_and_none_means_no_expiry() -> None:
    hub = PostgresPersistence(application_id="tests", connection_string="host=h", retention=DAY)
    try:
        assert hub.session_store().retention == DAY
        assert hub.checkpoint_storage().retention == DAY
        assert hub.history_provider().retention == DAY
        # As in 0.1.1, an explicit None gives the store its own default: nothing expires.
        assert hub.session_store(retention=None).retention.ttl is None
        assert hub.history_provider(retention=HOUR).retention == HOUR
    finally:
        await hub.close()
