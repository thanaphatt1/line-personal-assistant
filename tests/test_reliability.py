from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import pytest

from line_assistant.adapters import Conflict


def test_redelivery_replays_receipt_and_changed_event_id_payload_is_rejected(h):
    event = h.event("BTS 65")
    first = h.service.handle(event)
    event["deliveryContext"] = {"isRedelivery": True}
    assert h.service.handle(event) == first
    assert len(h.service.sheets.entries) == 1
    event["message"]["text"] = "BTS 100"
    assert h.service.handle(event).status == "conflict"
    assert len(h.service.sheets.entries) == 1


def test_identical_new_purchases_have_distinct_keys(h):
    assert h.send("BTS 65").status == "recorded"
    assert h.send("BTS 65").status == "recorded"
    assert len(h.service.sheets.entries) == 2


def test_concurrent_redelivery_commits_once_in_one_process(h):
    event = h.event("BTS 65")
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(h.service.handle, [event] * 20))
    assert all(result.status == "recorded" for result in results)
    assert len(h.service.sheets.entries) == 1


@pytest.mark.parametrize("kind", ["expense", "create", "update", "delete", "clarified_expense"])
def test_failure_after_mock_commit_reconciles_without_duplicate_side_effects(h, kind):
    if kind in {"update", "delete"}:
        h.send("นัด ตรวจฟัน | 2026-10-11 14:00")
        proposal = h.send("แก้นัด ล่าสุด | ใหม่" if kind == "update" else "ลบนัด ล่าสุด")
        event = h.event(f"ยืนยัน {proposal.data['token']}")
        provider, method = h.service.calendar, kind
    elif kind == "clarified_expense":
        proposal = h.send("sevne 100")
        event = h.event(f"เลือก {proposal.data['token']} seven")
        provider, method = h.service.sheets, "add"
    elif kind == "create":
        event = h.event("นัด ตรวจฟัน | 2026-10-11 14:00")
        provider, method = h.service.calendar, "create"
    else:
        event = h.event("BTS 65")
        provider, method = h.service.sheets, "add"
    original = getattr(provider, method)
    failed = False

    def commit_then_fail(*args, **kwargs):
        nonlocal failed
        receipt = original(*args, **kwargs)
        if not failed:
            failed = True
            raise RuntimeError("Simulated loss of acknowledgement after a committed mock write")
        return receipt

    setattr(provider, method, commit_then_fail)
    with pytest.raises(RuntimeError):
        h.service.handle(event)
    assert event["webhookEventId"] not in h.service.state.receipts
    result = h.service.handle(event)
    assert result.status == {"expense": "recorded", "create": "created", "update": "updated",
                             "delete": "deleted", "clarified_expense": "recorded"}[kind]
    if kind in {"expense", "clarified_expense"}:
        assert len(h.service.sheets.entries) == 1
    elif kind == "delete":
        assert not h.service.calendar.events
    else:
        assert len(h.service.calendar.events) == 1
        if kind == "update":
            assert next(iter(h.service.calendar.events.values())).version == 2


def test_provider_idempotency_key_is_bound_to_payload(h):
    h.send("BTS 65")
    key = next(iter(h.service.sheets.effects.receipts))
    changed = h.service.sheets.entries[0].model_copy(update={"amount": "100"})
    with pytest.raises(Conflict):
        h.service.sheets.add([changed], key)
    assert len(h.service.sheets.entries) == 1


def test_stale_unseen_event_and_expired_receipt_cannot_write_again(h):
    event = h.event("BTS 65")
    h.service.handle(event)
    h.now += timedelta(seconds=h.service.settings.webhook_ttl_seconds + 1)
    assert h.service.handle(event).status == "stale"
    assert len(h.service.sheets.entries) == 1
    assert not h.service.state.receipts


def test_old_pending_receipt_replay_does_not_recreate_pending(h):
    event = h.event("sevne 100")
    pending = h.service.handle(event)
    h.send("ยกเลิก")
    assert h.service.handle(event) == pending
    assert h.service.state.pending is None
    assert h.send(f"เลือก {pending.data['token']} seven").status == "no_pending"


def test_future_event_is_not_accepted(h):
    future = int((h.now + timedelta(minutes=6)).timestamp() * 1000)
    assert h.send("BTS 65", stamp=future).status == "stale"
    assert not h.service.sheets.entries
