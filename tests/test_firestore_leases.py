from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json

import httpx
import pytest

from line_assistant.api_sandbox import api_sandbox
from line_assistant.firestore_state import FirestoreState, LeaseBusy, LeaseLost
from line_assistant.mock_api import MockApiServices
from line_assistant.state import Pending
from line_assistant.transport import OutcomeUnknown, RestClient


NOW = datetime(2026, 10, 11, 2, tzinfo=timezone.utc)


def state(providers, clock=lambda: NOW, owner="mock-owner", **kwargs):
    return FirestoreState(RestClient("firestore", providers.transport()), owner, project_id="synthetic-project",
                           clock=clock, **kwargs)


def event():
    return {"type": "message", "webhookEventId": "purchase", "timestamp": int(NOW.timestamp()*1000),
            "source": {"type": "user", "userId": "mock-owner"},
            "message": {"type": "text", "id": "message", "text": "BTS 65"}}


def test_conditional_state_lease_serializes_holders_and_persists_pending():
    providers = MockApiServices()
    first, second = state(providers), state(providers)
    with first.session():
        first.pending = Pending("item", "deadbeef", "origin", 1, NOW + timedelta(minutes=30), {"item": "sevne"})
        first.checkpoint()
        with pytest.raises(LeaseBusy):
            with second.session():
                pytest.fail("An active holder must be exclusive")
    with second.session():
        assert second.pending.token == "deadbeef"
        assert second.fence == 2
    document = next(iter(providers.documents.values()))
    assert document["fields"]["lease_owner"]["stringValue"] == ""
    assert document["name"].startswith("projects/synthetic-project/databases/(default)/documents/")


def test_expired_worker_is_fenced_from_checkpoints_and_before_effect_after_takeover():
    providers = MockApiServices()
    current = [NOW]
    first, second = state(providers, clock=lambda: current[0]), state(providers, clock=lambda: current[0])
    with first.session():
        first.pending = Pending("item", "deadbeef", "origin", 1, NOW + timedelta(minutes=30), {})
        first.checkpoint()
        current[0] += timedelta(seconds=121)
        with second.session():
            assert second.pending.token == "deadbeef"
            second.pending = None
            second.checkpoint()
            before = deepcopy(providers.documents)
            with pytest.raises(LeaseLost):
                first.checkpoint()
            with pytest.raises(LeaseLost):
                first.before_effect()
            assert providers.documents == before
    with state(providers, clock=lambda: current[0]).session():
        pass
    stored = json.loads(next(iter(providers.documents.values()))["fields"]["snapshot"]["stringValue"])
    assert stored["pending"] is None


def test_clock_expiry_without_takeover_still_prevents_new_effect():
    providers = MockApiServices()
    current = [NOW]
    holder = state(providers, clock=lambda: current[0])
    with holder.session():
        current[0] += timedelta(seconds=121)
        with pytest.raises(LeaseLost):
            holder.before_effect()
    assert providers.sheet_writes == 0


def test_checkpoints_use_acknowledged_versions_without_another_lease_read():
    providers = MockApiServices()
    holder = state(providers)
    with holder.session():
        providers.calls.clear()
        versions = [holder.document['updateTime']]
        for _ in range(3):
            holder.checkpoint()
            versions.append(holder.document['updateTime'])
        assert len(set(versions)) == 4
        assert all(method == 'POST' for method, _, _ in providers.calls)
    # Release uses the same version condition; the next session still reads fresh.
    assert len(providers.calls) == 4
    with state(providers).session():
        pass
    assert sum(method == 'GET' for method, _, _ in providers.calls) == 1


@pytest.mark.parametrize('change', ['version', 'holder', 'deleted'])
def test_remote_changes_fence_cached_checkpoints_even_before_local_lease_expiry(change):
    providers = MockApiServices()
    holder = state(providers)
    with holder.session():
        remote = providers.documents[holder.name]
        if change == 'deleted':
            del providers.documents[holder.name]
        else:
            remote['updateTime'] = '2026-10-11T00:00:01Z'
            if change == 'holder':
                remote['fields']['lease_owner']['stringValue'] = 'another-worker'
                remote['fields']['fence']['integerValue'] = str(holder.fence + 1)
        before = deepcopy(providers.documents)
        with pytest.raises(LeaseLost):
            holder.before_effect()
        assert providers.documents == before
    assert providers.documents == before  # Release cannot overwrite the newer state.


def test_checkpoint_lost_ack_reads_back_once_then_uses_the_acknowledged_version():
    providers = MockApiServices()
    holder = state(providers)
    with holder.session():
        providers.calls.clear()
        providers.fail_after_firestore_commit = True
        holder.checkpoint()
        assert [method for method, _, _ in providers.calls] == ['POST', 'GET']
        version = holder.document['updateTime']
        holder.before_effect()
        assert holder.document['updateTime'] != version
        assert [method for method, _, _ in providers.calls] == ['POST', 'GET', 'POST']


def test_unverified_checkpoint_cannot_authorize_effect_or_overwrite_a_committed_version():
    providers = MockApiServices()
    broken = [False]
    def transport(request):
        if broken[0] and request.method == 'GET':
            raise httpx.ReadTimeout('Readback unavailable', request=request)
        result = providers.handle(request)
        if broken[0] and request.method == 'POST' and result.status_code == 200:
            raise httpx.ReadTimeout('Committed without acknowledgement', request=request)
        return result
    holder = FirestoreState(RestClient('firestore', httpx.MockTransport(transport)),
                            'mock-owner', clock=lambda: NOW)
    with holder.session():
        broken[0] = True
        with pytest.raises(OutcomeUnknown):
            holder.checkpoint()
        committed = deepcopy(providers.documents)
        with pytest.raises(LeaseLost):
            holder.before_effect()
    assert providers.documents == committed
    assert providers.sheet_writes == 0


def test_lease_expiring_during_checkpoint_does_not_authorize_a_new_effect():
    providers = MockApiServices()
    current, delayed = [NOW], [False]
    def transport(request):
        result = providers.handle(request)
        if delayed[0] and request.method == 'POST':
            current[0] += timedelta(seconds=121)
        return result
    holder = FirestoreState(RestClient('firestore', httpx.MockTransport(transport)),
                            'mock-owner', clock=lambda: current[0])
    with holder.session():
        delayed[0] = True
        with pytest.raises(LeaseLost):
            holder.before_effect()
    assert providers.sheet_writes == 0


def test_commit_nonce_readback_resolves_lost_ack_and_cas_race_refuses_stale_write():
    providers = MockApiServices()
    holder = state(providers)
    providers.fail_after_firestore_commit = True
    with holder.session():
        first_version = holder.document["updateTime"]
        holder.checkpoint()
        assert holder.document["updateTime"] != first_version
        old = deepcopy(holder.document)
        holder.checkpoint()
        with pytest.raises(LeaseLost):
            holder._commit(old, holder._fields({}, holder.holder, holder.fence, NOW + timedelta(seconds=120)))


def test_lost_ack_readback_accepts_server_canonicalized_rfc3339_timestamp():
    providers = MockApiServices()

    def canonical(request):
        response = providers.handle(request)
        if request.method == "POST":
            document = next(iter(providers.documents.values()))
            lease = document["fields"]["lease_until"]["timestampValue"]
            document["fields"]["lease_until"]["timestampValue"] = lease.replace("+00:00", "Z")
            raise httpx.ReadTimeout("Committed with canonical timestamp", request=request)
        return response

    holder = FirestoreState(RestClient("firestore", httpx.MockTransport(canonical)), "mock-owner", clock=lambda: NOW)
    with holder.session():
        holder.checkpoint()
    assert next(iter(providers.documents.values()))["fields"]["lease_owner"]["stringValue"] == ""


def test_unresolved_commit_acknowledgement_blocks_provider_io():
    providers = MockApiServices()
    writes = 0

    def broken(request):
        nonlocal writes
        if request.method == "GET":
            if writes:
                raise httpx.ReadTimeout("Readback unavailable", request=request)
            return providers.handle(request)
        writes += 1
        providers.handle(request)
        raise httpx.ReadTimeout("Acknowledgement unavailable", request=request)

    holder = FirestoreState(RestClient("firestore", httpx.MockTransport(broken)), "mock-owner", clock=lambda: NOW)
    with pytest.raises(OutcomeUnknown):
        with holder.session():
            pytest.fail("Unverified acquisition must not authorize work")
    assert providers.sheet_writes == 0


def test_snapshot_size_guard_and_owner_namespaces():
    providers = MockApiServices()
    with pytest.raises(OutcomeUnknown, match="capacity"):
        with state(providers, max_snapshot_bytes=10).session():
            pytest.fail("Oversized state must not reach provider writes")
    for owner in ("mock-owner", "another-owner"):
        with state(providers, owner=owner).session():
            pass
    assert len(providers.documents) == 2


def test_lease_takeover_before_rpc_prevents_old_worker_sending_a_sheet_write():
    providers = MockApiServices()
    current = [NOW]
    first, _, _ = api_sandbox(services=providers, clock=lambda: current[0])
    second, _, _ = api_sandbox(services=providers, clock=lambda: current[0])

    def takeover(phase, key):
        if phase == "executing":
            current[0] += timedelta(seconds=121)
            assert second.handle(event()).status == "uncertain"

    first.phase_hook = takeover
    with pytest.raises(LeaseLost):
        first.handle(event())
    assert providers.sheet_writes == 0
    assert second.handle(event()).status == "uncertain"


def test_inflight_old_write_cannot_be_fenced_but_new_worker_does_not_duplicate_it():
    providers = MockApiServices()
    current = [NOW]
    first, _, _ = api_sandbox(services=providers, clock=lambda: current[0])
    second, _, _ = api_sandbox(services=providers, clock=lambda: current[0])

    def takeover_after_commit(mock):
        current[0] += timedelta(seconds=121)
        assert second.handle(event()).status == "uncertain"

    providers.after_sheet_write = takeover_after_commit
    with pytest.raises(LeaseLost):
        first.handle(event())
    assert providers.sheet_writes == 1
    assert second.handle(event()).status == "uncertain"
    fresh = event()
    fresh["webhookEventId"] = "different"
    fresh["message"]["id"] = "different-message"
    assert second.handle(fresh).status == "uncertain"
    assert providers.sheet_writes == 1
