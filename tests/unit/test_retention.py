import copy
import dataclasses
import pickle
from datetime import timedelta

import pytest

from agent_framework_community_postgres._retention import EXPIRES_AT, PurgeReport, RetentionPolicy


def test_policy_defaults_to_off() -> None:
    policy = RetentionPolicy()
    assert policy.ttl is None
    assert policy.mode == "tombstone"
    assert not policy.enabled
    assert RetentionPolicy(ttl=timedelta(days=30)).enabled


@pytest.mark.parametrize("ttl", [timedelta(0), timedelta(seconds=-1)])
def test_policy_rejects_non_positive_ttl(ttl: timedelta) -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(ttl=ttl)


def test_policy_rejects_unknown_mode() -> None:
    with pytest.raises(ValueError):
        RetentionPolicy(mode="archive")  # type: ignore[arg-type]


def test_purge_report_adds_and_totals() -> None:
    report = PurgeReport({"af_documents": 2}) + PurgeReport({"af_sessions": 3, "af_documents": 1})
    assert report.counts == {"af_documents": 3, "af_sessions": 3}
    assert report.total == 6


def test_expires_at_fragment_uses_the_ttl_parameter_twice() -> None:
    assert EXPIRES_AT.as_string().count("%(ttl)s") == 2


def test_purge_reports_sum_without_a_start_value() -> None:
    total = sum([PurgeReport({"af_documents": 2}), PurgeReport({"af_sessions": 3})])
    assert isinstance(total, PurgeReport)
    assert total.counts == {"af_documents": 2, "af_sessions": 3}


def test_purge_report_counts_are_a_read_only_copy() -> None:
    source = {"af_documents": 1}
    report = PurgeReport(source)
    source["af_documents"] = 5
    assert report.counts == {"af_documents": 1}
    with pytest.raises(TypeError):
        report.counts["af_documents"] = 2  # type: ignore[index]


def test_purge_report_pickles_and_copies() -> None:
    report = PurgeReport({"af_documents": 2, "af_sessions": 0})
    for clone in (pickle.loads(pickle.dumps(report)), copy.deepcopy(report), copy.copy(report)):
        assert clone == report
        assert clone.total == 2
        with pytest.raises(TypeError):
            clone.counts["af_documents"] = 3  # type: ignore[index]


@pytest.mark.parametrize(
    "mutate",
    [
        lambda counts: counts.__setitem__("af_documents", 2),
        lambda counts: counts.__delitem__("af_documents"),
        lambda counts: counts.update({"af_sessions": 1}),
        lambda counts: counts.pop("af_documents"),
        lambda counts: counts.popitem(),
        lambda counts: counts.setdefault("af_sessions", 1),
        lambda counts: counts.clear(),
    ],
)
def test_purge_report_counts_refuse_every_change(mutate: object) -> None:
    report = PurgeReport({"af_documents": 1})
    with pytest.raises(TypeError):
        mutate(report.counts)  # type: ignore[operator]
    assert report.counts == {"af_documents": 1}


def test_purge_report_works_with_asdict() -> None:
    assert dataclasses.asdict(PurgeReport({"af_documents": 1})) == {"counts": {"af_documents": 1}}
    assert PurgeReport({"a": 1}) == PurgeReport({"a": 1}) != PurgeReport({"a": 2})
