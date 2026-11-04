"""Read-only smoke tests for every declared node.

These run with only ``RCF_IT=1``.  They inspect real nodes and never submit,
cancel, create or delete anything on them.
"""

from __future__ import annotations

import pytest
from _support import enabled, make_service, selected_nodes

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not enabled(), reason="set RCF_IT=1 to run integration tests"),
]


@pytest.fixture(scope="module")
def service():
    svc = make_service()
    yield svc
    svc.close()


@pytest.fixture(scope="module")
def nodes(service):
    return selected_nodes(service)


def test_declared_nodes_are_reachable(service, nodes):
    for node_id in nodes:
        payload = service.inspect_node(node_id, refresh=True)
        snapshot = payload["snapshot"]

        assert payload["node"]["id"] == node_id
        assert snapshot["observed_at"], f"{node_id}: probe did not report an observation time"
        host = next((check for check in snapshot["checks"] if check["name"] == "host"), None)
        assert host is not None, f"{node_id}: no host check in probe result"
        assert host["status"] != "fail", f"{node_id}: {host['detail']}"

        if payload["node"]["kind"] == "slurm":
            scheduler = snapshot["scheduler"]
            assert scheduler["available"] is True, f"{node_id}: scheduler unavailable"
            assert scheduler["partitions"], f"{node_id}: no Slurm partitions reported"


def test_doctor_reports_a_usable_home(service):
    report = service.doctor()

    assert report["ok"] is True
    assert report["home"]
    assert report["database"]
    assert any(check["name"] == "nodes" for check in report["checks"])
