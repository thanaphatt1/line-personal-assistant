"""Bounded timed-event Calendar REST adapter; configured resource only."""

from datetime import datetime
import hashlib
from urllib.parse import quote

from .adapters import CalendarEvent, Conflict, NotFound, Reconciliation
from .contracts import CreateEvent
from .google_requests import create_event_preview, delete_event_preview, update_event_preview
from .transport import ApiError, NotReady, OutcomeUnknown, RestClient, WriteBlocked


def decode_event(value):
    if "dateTime" not in value.get("start", {}) or "dateTime" not in value.get("end", {}):
        raise NotReady("All-day event handling is unresolved; do not omit it from an agenda")
    start = datetime.fromisoformat(value["start"]["dateTime"].replace("Z", "+00:00"))
    end = datetime.fromisoformat(value["end"]["dateTime"].replace("Z", "+00:00"))
    if start.tzinfo is None or end.tzinfo is None or not value.get("etag"):
        raise NotReady("Event offsets/ETag missing")
    reminders = value.get("reminders", {"useDefault": True})
    minutes = next((item["minutes"] for item in reminders.get("overrides", []) if item.get("method") == "popup"), None)
    event = CalendarEvent(value["id"], value.get("summary", "(ไม่มีชื่อ)"), start, end, minutes, value["etag"], reminders)
    return event


class CalendarApi:
    def __init__(self, client: RestClient, calendar_id="placeholder-calendar", *, writes_enabled=False, effect_guard=None):
        if client.service != "calendar" or not calendar_id:
            raise ValueError("Configured Calendar transport/target required")
        self.client, self.calendar_id, self.writes_enabled = client, calendar_id, writes_enabled
        self.base = f"/calendar/v3/calendars/{quote(calendar_id, safe='')}/events"
        self.effect_guard = effect_guard or (lambda: None)

    def _request(self, method, path, **kwargs):
        try:
            return self.client.request(method, path, **kwargs)
        except ApiError as error:
            if error.status in {404, 410}:
                raise NotFound("Target absent from configured calendar") from None
            if error.status == 412:
                raise Conflict("ETag changed; require a new review") from None
            raise

    def _write(self):
        if not self.writes_enabled:
            raise WriteBlocked("Calendar target/default decisions and write setup are unresolved")
        self.effect_guard()

    def get(self, event_id):
        return decode_event(self._request("GET", self.base + "/" + quote(event_id, safe="")))

    def list(self, start, end):
        result, page, seen = [], None, set()
        for _ in range(20):
            params = {"timeMin": start.isoformat(), "timeMax": end.isoformat(), "singleEvents": "true",
                      "orderBy": "startTime", "timeZone": "Asia/Bangkok", "maxResults": 250}
            if page:
                params["pageToken"] = page
            response = self._request("GET", self.base, params=params)
            result.extend(decode_event(item) for item in response.get("items", []) if item.get("status") != "cancelled")
            page = response.get("nextPageToken")
            if not page:
                return result
            if page in seen:
                raise NotReady("Repeated Calendar page token")
            seen.add(page)
        raise NotReady("Agenda exceeds bounded page limit; do not present a partial agenda")

    def create(self, args, key):
        preview = create_event_preview(args, key)
        self._write()
        try:
            response = self.client.request("POST", self.base, body=preview.body)
        except ApiError as error:
            if error.status != 409:
                raise
            proof = self.reconcile(key, args.model_dump(mode="json"))
            if proof.outcome != "committed":
                raise Conflict("Existing event ID is not the frozen action") from None
            return proof.receipt
        event = decode_event(response)
        if (event.id != preview.body["id"] or event.title != args.title or event.start != args.start
                or event.end != args.end or event.native_reminders != preview.body["reminders"]):
            raise OutcomeUnknown("Created event acknowledgement does not match the action")
        return event

    def update(self, event_id, args, version, key):
        preview = update_event_preview(args, event_id, version)
        self._write()
        event = decode_event(self._request("PATCH", self.base + "/" + quote(event_id, safe=""),
                                          body=preview.body, headers=preview.headers))
        if (event.id != event_id or (args.title is not None and event.title != args.title)
                or (args.start is not None and (event.start != args.start or event.end != args.end))):
            raise OutcomeUnknown("Edited event acknowledgement does not match the action")
        return event

    def delete(self, event_id, version, key):
        preview = delete_event_preview(event_id, version)
        self._write()
        self._request("DELETE", self.base + "/" + quote(event_id, safe=""), headers=preview.headers)
        return event_id

    def reconcile(self, key, payload):
        if "title" not in payload or "start" not in payload:
            return Reconciliation("unknown")  # Final values/absence do not prove edit/delete authorship.
        args = CreateEvent.model_validate(payload)
        try:
            event = self.get(hashlib.sha256(key.encode()).hexdigest())
        except NotFound:
            return Reconciliation("unknown")  # Insert may still be in flight.
        wanted = create_event_preview(args, key).body
        if (event.title != args.title or event.start != args.start or event.end != args.end
                or event.native_reminders != wanted["reminders"]):
            return Reconciliation("unknown")
        return Reconciliation("committed", event)
