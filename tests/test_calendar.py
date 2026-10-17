from datetime import datetime, timedelta, timezone

import pytest

from line_assistant.contracts import CreateEvent, DeleteEvent, ListEvents, UpdateEvent


def create(h):
    return h.send("นัด ตรวจฟัน | 2026-10-11 14:00 | 2026-10-11 15:00 | 30")


def test_create_agenda_and_native_reminder(h):
    result = create(h)
    assert result.status == "created"
    assert result.data["start"] == "2026-10-11T14:00:00+07:00"
    assert result.data["reminders"] == {"useDefault": False, "overrides": [{"method": "popup", "minutes": 30}]}
    assert h.send("วันนี้มีอะไรบ้าง").data["events"][0]["id"] == result.data["id"]


def test_calendar_time_clarification_and_defaults(h):
    pending = h.send("นัด ตรวจฟัน")
    assert pending.status == "clarification"
    assert not h.service.calendar.events
    result = h.send(f"เวลา {pending.data['token']} | 2026-10-11 14:00")
    assert result.status == "created"
    assert result.data["end"] == "2026-10-11T15:00:00+07:00"
    assert result.data["reminders"]["overrides"][0]["minutes"] == 30


def test_conflict_needs_current_owner_confirmation(h):
    create(h)
    proposed = h.send("นัด นัดสอง | 2026-10-11 14:30 | 2026-10-11 15:30")
    assert proposed.data["stage"] == "calendar_conflict"
    assert len(h.service.calendar.events) == 1
    token = proposed.data["token"]
    assert h.send(f"ยืนยัน {token}", owner="intruder").status == "denied"
    assert len(h.service.calendar.events) == 1
    assert h.send(f"ยืนยัน {token}").status == "created"
    assert len(h.service.calendar.events) == 2


def test_update_and_delete_require_confirmation(h):
    event_id = create(h).data["id"]
    update = h.send("แก้นัด ล่าสุด | ตรวจฟันใหม่")
    assert h.service.calendar.get(event_id).title == "ตรวจฟัน"
    assert h.send(f"ยืนยัน {update.data['token']}").status == "updated"
    assert h.service.calendar.get(event_id).title == "ตรวจฟันใหม่"
    deletion = h.send("ลบนัด ล่าสุด")
    assert event_id in h.service.calendar.events
    assert h.send(f"ยืนยัน {deletion.data['token']}").status == "deleted"
    assert event_id not in h.service.calendar.events
    assert h.send(f"ยืนยัน {deletion.data['token']}").status == "no_pending"


def test_model_cannot_assert_confirmation():
    with pytest.raises(ValueError):
        DeleteEvent.model_validate({"event_ref": "last", "confirmed": True})


def test_last_reference_expires_and_does_not_guess(h):
    create(h)
    h.now += timedelta(seconds=h.service.settings.last_action_ttl_seconds + 1)
    assert h.send("ลบนัด ล่าสุด").status == "not_found"
    assert len(h.service.calendar.events) == 1


def test_changed_target_cannot_be_deleted_using_old_confirmation(h):
    event_id = create(h).data["id"]
    pending = h.send("ลบนัด ล่าสุด")
    h.service.calendar.update(event_id, UpdateEvent(event_ref=event_id, title="changed externally"), 1, "external-edit")
    assert h.send(f"ยืนยัน {pending.data['token']}").status == "conflict"
    assert event_id in h.service.calendar.events


def test_delayed_confirmation_or_cancel_cannot_apply_to_new_pending(h):
    create(h)
    pending = h.send("ลบนัด ล่าสุด")
    stamp = h.service.state.pending.source_timestamp - 1
    assert h.send(f"ยืนยัน {pending.data['token']}", stamp=stamp).status == "stale"
    assert h.send("ยกเลิก", stamp=stamp).status == "stale"
    assert len(h.service.calendar.events) == 1


def test_today_uses_bangkok_boundary_not_utc_date(h):
    h.now = datetime(2026, 12, 31, 18, 0, tzinfo=timezone.utc)  # Jan 1 in Bangkok.
    h.send("BTS 65")
    assert h.service.sheets.entries[0].date.isoformat() == "2027-01-01"
    h.send("นัด ปีใหม่ | 2027-01-01 02:00 | 2027-01-01 03:00")
    h.send("นัด วานนี้ | 2026-12-31 22:00 | 2026-12-31 23:00")
    assert [event["title"] for event in h.send("today").data["events"]] == ["ปีใหม่"]


@pytest.mark.parametrize("patch", [
    {"start": "2026-10-11T14:00:00"},
    {"end": "2026-10-11T13:00:00+07:00"},
    {"reminder_minutes": -1}, {"reminder_minutes": 40321},
    {"reminder_minutes": True}, {"calendar_id": "other"}, {"attendees": ["someone"]},
])
def test_invalid_or_unconfigured_calendar_arguments(patch):
    with pytest.raises(ValueError):
        CreateEvent.model_validate({"title": "test", "start": "2026-10-11T14:00:00+07:00",
                                   "end": "2026-10-11T15:00:00+07:00", **patch})


def test_query_windows_are_bounded():
    with pytest.raises(ValueError):
        ListEvents(start="2026-01-01T00:00:00+07:00", end="2027-01-01T00:00:00+07:00")


def test_demo_does_not_infer_midnight_from_date_without_time(h):
    assert h.send("นัด ตรวจฟัน | 2026-10-11").status == "invalid"
    assert not h.service.calendar.events
