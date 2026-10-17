import httpx
import pytest

from line_assistant.adapters import Conflict, NotFound
from line_assistant.calendar_api import CalendarApi
from line_assistant.contracts import CreateEvent, UpdateEvent
from line_assistant.mock_api import MockApiServices
from line_assistant.transport import NotReady, OutcomeUnknown, RestClient, WriteBlocked


def appointment():
    return CreateEvent.model_validate({"title": "ตรวจฟัน", "start": "2026-10-11T14:00:00+07:00",
                                      "end": "2026-10-11T15:00:00+07:00", "reminder_minutes": 30})


def calendar(providers, enabled=True):
    return CalendarApi(RestClient("calendar", providers.transport()), "synthetic-calendar", writes_enabled=enabled)


def test_create_get_list_update_delete_use_stable_ids_and_exact_etags():
    providers = MockApiServices()
    api = calendar(providers)
    created = api.create(appointment(), "server-action-key")
    assert created.version == '"version-1"'
    assert created.as_dict()["reminders"]["overrides"] == [{"method": "popup", "minutes": 30}]
    assert api.create(appointment(), "server-action-key").id == created.id
    assert len(providers.calendar_events) == 1
    assert api.get(created.id).title == "ตรวจฟัน"
    assert len(api.list(appointment().start, appointment().end)) == 1
    patch = UpdateEvent.model_validate({"event_ref": "last", "title": "ชื่อใหม่"})
    updated = api.update(created.id, patch, created.version, "update-key")
    assert updated.title == "ชื่อใหม่"
    with pytest.raises(Conflict):
        api.delete(created.id, created.version, "delete-key")
    assert api.delete(created.id, updated.version, "delete-key") == created.id
    with pytest.raises(NotFound):
        api.get(created.id)


def test_calendar_disabled_setup_and_missing_insert_are_not_claimed_as_negative_proof():
    providers = MockApiServices()
    api = calendar(providers, enabled=False)
    with pytest.raises(WriteBlocked):
        api.create(appointment(), "key")
    assert not providers.calendar_events
    assert api.reconcile("key", appointment().model_dump(mode="json")).outcome == "unknown"
    assert api.reconcile("delete-key", {"id": "missing", "version": '"v1"'}).outcome == "unknown"


def test_matching_create_id_is_positive_proof_and_changed_event_is_not():
    providers = MockApiServices()
    api = calendar(providers)
    created = api.create(appointment(), "key")
    assert api.reconcile("key", appointment().model_dump(mode="json")).outcome == "committed"
    providers.calendar_events[created.id]["summary"] = "Changed externally"
    assert api.reconcile("key", appointment().model_dump(mode="json")).outcome == "unknown"


def test_calendar_pagination_filters_cancelled_and_preserves_native_reminders():
    requests = []
    item = {"id": "event", "summary": "Existing", "etag": '"etag"',
            "start": {"dateTime": "2026-10-11T14:00:00+07:00"}, "end": {"dateTime": "2026-10-11T15:00:00+07:00"},
            "reminders": {"useDefault": False, "overrides": [{"method": "email", "minutes": 10}]}}

    def pages(request):
        requests.append(request)
        if request.url.params.get("pageToken"):
            return httpx.Response(200, json={"items": [item]})
        return httpx.Response(200, json={"items": [{**item, "status": "cancelled"}], "nextPageToken": "next"})

    api = CalendarApi(RestClient("calendar", httpx.MockTransport(pages)), "synthetic-calendar")
    listed = api.list(appointment().start, appointment().end)
    assert len(listed) == 1
    assert listed[0].as_dict()["reminders"] == item["reminders"]
    assert requests[0].url.params["singleEvents"] == "true"
    assert requests[0].url.params["timeZone"] == "Asia/Bangkok"


@pytest.mark.parametrize("problem", ["all_day", "page_loop"])
def test_unhandled_agenda_data_is_not_silently_presented_as_a_complete_list(problem):
    body = {"items": [{"id": "all-day", "start": {"date": "2026-10-11"}, "end": {"date": "2026-10-12"}}]} if problem == "all_day" else {"items": [], "nextPageToken": "same"}
    api = CalendarApi(RestClient("calendar", httpx.MockTransport(lambda request: httpx.Response(200, json=body))))
    with pytest.raises(NotReady):
        api.list(appointment().start, appointment().end)


def test_inconsistent_success_acknowledgement_is_not_reported_as_a_completed_create():
    providers = MockApiServices()

    def changed(request):
        response = providers.handle(request)
        if request.method == "POST":
            body = response.json()
            body["id"] = "different-id"
            return httpx.Response(200, json=body)
        return response

    api = CalendarApi(RestClient("calendar", httpx.MockTransport(changed)), "synthetic-calendar", writes_enabled=True)
    with pytest.raises(OutcomeUnknown):
        api.create(appointment(), "key")
    assert len(providers.calendar_events) == 1
