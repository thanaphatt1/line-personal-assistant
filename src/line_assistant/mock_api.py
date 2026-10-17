"""Synthetic REST protocol simulator for demos/tests, not an official emulator.

No network sockets or credentials. Call logs retain method/host/path, not tokens or
request bodies. Fault flags simulate committed writes with lost acknowledgements.
"""

from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import re
from threading import RLock
from urllib.parse import unquote

import httpx


def column_index(value):
    result = 0
    for character in value:
        result = result * 26 + ord(character) - ord("A") + 1
    return result


class MockApiServices:
    def __init__(self, *, id_column=None):
        self.lock = RLock()
        self.calls = []
        self.documents = {}
        self.version = 0
        self.cells = {}
        self.sheet_writes = 0
        self.line_replies = []
        self.calendar_events = {}
        self.protections = []
        self.database_protections = []
        self.reject_atomic_batch = False
        self.hidden_columns = {column_index(id_column) - 1} if id_column else set()
        self.row_count = 1000
        self.database_row_count = 996
        self.column_count = 24
        self.fail_after_sheet_write = False
        self.fail_after_reply = False
        self.fail_after_firestore_commit = False
        self.before_candidate_read = None
        self.before_sheet_write = None
        self.after_sheet_write = None
        for column, value in enumerate(("Date", "List", "Cost", "Remark"), start=1):
            self.cells[("All Expenses", 1, column)] = value
        for column, value in enumerate(("Item", "Categories", "Sub-categories"), start=1):
            self.cells[("Database", 1, column)] = value
        for row, item in enumerate(("BTS", "seven", "กาแฟ", "อาหาร"), start=2):
            self.cells[("Database", row, 1)] = item
            self.cells[("Database", row, 2)] = "Synthetic"
        if id_column:
            self.cells[("All Expenses", 1, column_index(id_column))] = "transaction_id"

    def transport(self):
        return httpx.MockTransport(self.handle)

    def handle(self, request):
        with self.lock:
            self.calls.append((request.method, request.url.host, request.url.path))
            if request.url.host == "firestore.googleapis.com":
                return self.firestore(request)
            if request.url.host == "sheets.googleapis.com":
                return self.sheets(request)
            if request.url.host == "api.line.me":
                assert request.method == "POST" and request.url.path == "/v2/bot/message/reply"
                body = json.loads(request.content)
                assert "X-Line-Retry-Key" not in request.headers
                self.line_replies.append(deepcopy(body["messages"]))  # Never store replyToken.
                if self.fail_after_reply:
                    self.fail_after_reply = False
                    raise httpx.ReadTimeout("Simulated reply acknowledgement loss", request=request)
                return httpx.Response(200, json={"sentMessages": [{"id": str(len(self.line_replies))}]})
            if request.url.host == "www.googleapis.com":
                return self.calendar(request)
            raise AssertionError("Unexpected provider/endpoint")

    def firestore(self, request):
        if request.method=='POST' and request.url.path.endswith(':runQuery'):
            parent=unquote(request.url.path.removeprefix('/v1/').removesuffix(':runQuery'))
            q=json.loads(request.content)['structuredQuery']
            after=datetime.fromisoformat(q['where']['fieldFilter']['value']['timestampValue'].replace('Z','+00:00'))
            docs=[deepcopy(d) for name,d in self.documents.items() if name.startswith(parent+'/assistant_raw_messages/') and datetime.fromisoformat(d['fields']['at']['timestampValue'].replace('Z','+00:00'))>after]
            docs.sort(key=lambda d:(datetime.fromisoformat(d['fields']['at']['timestampValue'].replace('Z','+00:00')),d['name']))
            if q.get('startAt'):
                v=q['startAt']['values'];cursor=(datetime.fromisoformat(v[0]['timestampValue'].replace('Z','+00:00')),v[1]['referenceValue'])
                docs=[d for d in docs if (datetime.fromisoformat(d['fields']['at']['timestampValue'].replace('Z','+00:00')),d['name'])>cursor]
            return httpx.Response(200,json=[{'document':d} for d in docs[:q['limit']]])
        if request.method == "GET":
            name = unquote(request.url.path.removeprefix("/v1/"))
            document = self.documents.get(name)
            return httpx.Response(200, json=deepcopy(document)) if document else httpx.Response(404, json={})
        assert request.method == "POST" and request.url.path.endswith("/documents:commit")
        writes = json.loads(request.content)["writes"]
        assert len(writes) == 1
        update, condition = writes[0]["update"], writes[0]["currentDocument"]
        previous = self.documents.get(update["name"])
        if ((condition == {"exists": False} and previous) or
                ("updateTime" in condition and (not previous or previous["updateTime"] != condition["updateTime"]))):
            return httpx.Response(409, json={"error": {"status": "ABORTED"}})
        self.version += 1
        timestamp = (datetime(2026, 10, 11, tzinfo=timezone.utc) + timedelta(microseconds=self.version)).isoformat().replace("+00:00", "Z")
        self.documents[update["name"]] = {**deepcopy(update), "updateTime": timestamp}
        if self.fail_after_firestore_commit:
            self.fail_after_firestore_commit = False
            raise httpx.ReadTimeout("Simulated state acknowledgement loss", request=request)
        return httpx.Response(200, json={"writeResults": [{"updateTime": timestamp}], "commitTime": timestamp})

    @staticmethod
    def coordinates(value):
        tab, cells = value.rsplit("!", 1)
        tab = tab.strip("'").replace("''", "'")
        match = re.fullmatch(r"([A-Z]+)([0-9]+):([A-Z]+)([0-9]+)", cells)
        assert match, value
        return tab, column_index(match[1]), int(match[2]), column_index(match[3]), int(match[4])

    def range_values(self, value):
        tab, first_col, first_row, last_col, last_row = self.coordinates(value)
        rows = []
        for row in range(first_row, last_row + 1):
            values = [self.cells.get((tab, row, col), "") for col in range(first_col, last_col + 1)]
            while values and values[-1] == "":
                values.pop()
            rows.append(values)
        while rows and not rows[-1]:
            rows.pop()
        return rows

    def sheets(self, request):
        if request.method == "GET" and "/values/" not in request.url.path:
            return httpx.Response(200, json={"spreadsheetId": "synthetic-sheet", "sheets": [
                {"properties": {"title": "All Expenses", "sheetId": 101, "gridProperties": {"rowCount": self.row_count, "columnCount": self.column_count}},
                 "protectedRanges": deepcopy(self.protections),
                 "data": [{"startColumn": 0, "columnMetadata": [{"hiddenByUser": index in self.hidden_columns}
                                                                  for index in range(self.column_count)]}]},
                {"properties": {"title": "Database", "sheetId": 102, "gridProperties": {"rowCount": self.database_row_count, "columnCount": 26}},
                 "protectedRanges": deepcopy(self.database_protections)},
            ]})
        if request.method == "GET":
            value_range = unquote(request.url.path.split("/values/", 1)[1])
            if self.before_candidate_read and request.url.params.get("valueRenderOption") == "FORMULA":
                callback, self.before_candidate_read = self.before_candidate_read, None
                callback(self)
            return httpx.Response(200, json={"range": value_range, "majorDimension": "ROWS", "values": self.range_values(value_range)})
        assert request.method == "POST"
        if self.before_sheet_write:
            callback, self.before_sheet_write = self.before_sheet_write, None
            callback(self)
        body = json.loads(request.content)
        if request.url.path.endswith(":batchUpdate") and not request.url.path.endswith("/values:batchUpdate"):
            assert 1 <= len(body["requests"]) <= 2
            if self.reject_atomic_batch:
                return httpx.Response(400, json={'error': {'status': 'INVALID_ARGUMENT'}})
            staged = deepcopy(self.cells)
            for block in body['requests']:
                append = block['appendCells']
                assert append['sheetId'] in {101, 102} and append['fields'] == 'userEnteredValue'
                tab_name = 'All Expenses' if append['sheetId'] == 101 else 'Database'
                last = max((row for (tab, row, col), value in staged.items()
                            if tab == tab_name and value not in ('', None)), default=0)
                for offset, row in enumerate(append['rows'], start=1):
                    for column, cell in enumerate(row['values'], start=1):
                        if 'userEnteredValue' in cell:
                            staged[(tab_name, last + offset, column)] = next(iter(cell['userEnteredValue'].values()))
                if tab_name == 'All Expenses': self.row_count = max(self.row_count, last + len(append['rows']))
            self.cells = staged
            response = {"spreadsheetId": "synthetic-sheet", "replies": [{} for _ in body['requests']]}
        else:
            assert request.url.path.endswith("/values:batchUpdate")
            response = self._values_write(body)
        self.sheet_writes += 1
        if self.after_sheet_write:
            callback, self.after_sheet_write = self.after_sheet_write, None
            callback(self)
        if self.fail_after_sheet_write:
            self.fail_after_sheet_write = False
            raise httpx.ReadTimeout("Simulated sheet acknowledgement loss", request=request)
        return httpx.Response(200, json=response)

    def _values_write(self, body):
        assert body["valueInputOption"] == "RAW"
        replies = []
        for block in body["data"]:
            tab, first_col, first_row, _, _ = self.coordinates(block["range"])
            for row_offset, row in enumerate(block["values"]):
                for column_offset, value in enumerate(row):
                    self.cells[(tab, first_row + row_offset, first_col + column_offset)] = value
            replies.append({"updatedData": {"range": block["range"], "values": deepcopy(block["values"])}, "updatedRange": block["range"]})
        return {"spreadsheetId": "synthetic-sheet", "responses": replies}

    def calendar(self, request):
        base = "/calendar/v3/calendars/synthetic-calendar/events"
        assert request.url.path.startswith(base)
        event_id = unquote(request.url.path[len(base):].lstrip("/"))
        if request.method == "GET" and not event_id:
            return httpx.Response(200, json={"items": list(deepcopy(self.calendar_events).values())})
        previous = self.calendar_events.get(event_id)
        if request.method == "GET":
            return httpx.Response(200, json=deepcopy(previous)) if previous else httpx.Response(404, json={})
        if request.method == "POST":
            body = json.loads(request.content)
            if body["id"] in self.calendar_events:
                return httpx.Response(409, json={})
            event = {**body, "etag": '"version-1"'}
            self.calendar_events[event["id"]] = event
            return httpx.Response(200, json=deepcopy(event))
        if not previous:
            return httpx.Response(404, json={})
        if request.headers.get("If-Match") != previous["etag"]:
            return httpx.Response(412, json={})
        if request.method == "PATCH":
            event = {**previous, **json.loads(request.content), "etag": '"version-2"'}
            self.calendar_events[event_id] = event
            return httpx.Response(200, json=deepcopy(event))
        assert request.method == "DELETE"
        del self.calendar_events[event_id]
        return httpx.Response(204)
