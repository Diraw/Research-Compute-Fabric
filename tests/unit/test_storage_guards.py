"""A guard has no expiry, so an interrupted operation must be recoverable.

These tests cover the failure that a real interrupted fetch produced: two guards
left behind, every later fetch refused, and no supported way to clear them.  The
remedy is a legible conflict plus an explicit, audited release — never an automatic
takeover, because a guard whose owner is alive is the exclusion that keeps a
cleanup away from a running copy.
"""

from __future__ import annotations

import pytest
from conftest import make_home

from rcf.core.config import Config
from rcf.core.errors import Conflict, NotFound, PermissionDenied
from rcf.service import RcfService


@pytest.fixture
def service(tmp_path):
    home = make_home(tmp_path, nodes='[nodes.local]\nkind="local"\n')
    svc = RcfService(Config.load(home), actor="test")
    try:
        yield svc
    finally:
        svc.close()


def hold_guard(service, roots=("/data/out",), operation="fetch", owner="run_a", node="local"):
    return service.storage.ledger.acquire_guard(node, list(roots), operation=operation, owner_id=owner)


def test_a_blocked_acquisition_names_the_holder_and_the_command_that_frees_it(service):
    guard = hold_guard(service)

    with pytest.raises(Conflict) as caught:
        hold_guard(service, owner="run_b")

    message = str(caught.value)
    assert "fetch of run_a" in message
    assert "still holds these roots" in message
    assert guard in caught.value.hint
    assert "rcf storage unguard" in caught.value.hint


def test_releasing_a_guard_needs_explicit_confirmation(service):
    guard = hold_guard(service)

    with pytest.raises(PermissionDenied, match="confirmation"):
        service.storage.release_guard(guard, confirm=False)

    assert [item["guard_id"] for item in service.storage.guards()] == [guard]


def test_released_guards_stop_blocking_and_are_audited(service):
    guard = hold_guard(service)

    released = service.storage.release_guard(guard, confirm=True)

    assert released["owner_id"] == "run_a"
    assert service.storage.guards() == []
    audits = [row for row in service.store.list_audits(limit=20)
              if row["operation"] == "storage.release_guard"]
    assert audits and audits[0]["detail"]["guard_id"] == guard
    # The roots are usable again, which is the whole point of releasing.
    assert hold_guard(service, owner="run_b") != guard


def test_releasing_a_guard_that_is_not_active_is_not_found(service):
    with pytest.raises(NotFound, match="no active storage guard"):
        service.storage.release_guard("guard_does_not_exist", confirm=True)


def test_a_released_guard_cannot_be_released_twice(service):
    guard = hold_guard(service)
    service.storage.release_guard(guard, confirm=True)

    with pytest.raises(NotFound):
        service.storage.release_guard(guard, confirm=True)
