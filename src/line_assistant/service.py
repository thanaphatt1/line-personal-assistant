from copy import deepcopy
from datetime import date, datetime, timedelta, timezone
import hashlib
import json
import re
import secrets
from typing import Callable

from pydantic import ValidationError

from .adapters import Calendar, Conflict, NotFound, Sheets
from .catalog import ItemCatalog
from .config import Settings
from .contracts import (
    AddTransactions, CreateEvent, DeleteEvent, ListEvents, SpendingSummary,
    ToolRequest, UpdateEvent,
)
from .planner import CalendarClarification, Planner, PlannerNotice, RulePlanner
from .language import (ExpenseDraft, ExpensePreview, amounts, dates,
                       intent_guard, item_spans, read_request)
from .state import Action, Claim, LastAction, MemoryState, Pending, Receipt, Result
from .transport import WriteBlocked
from .events import fingerprint as event_fingerprint


class ProviderFailure(RuntimeError):
    """An attempted side effect has no trustworthy acknowledgement yet."""


class Assistant:
    def __init__(self, settings: Settings, sheets: Sheets, calendar: Calendar,
                 catalog: ItemCatalog, planner: Planner | None = None,
                 state: MemoryState | None = None,
                 clock: Callable[[], datetime] | None = None,
                 phase_hook: Callable[[str, str], None] | None = None,
                 expense_label: str = "จำลอง"):
        self.settings = settings
        self.sheets = sheets
        self.calendar = calendar
        self.catalog = catalog
        self.planner = planner or RulePlanner()
        self.state = state or MemoryState()
        self.clock = clock or (lambda: datetime.now(timezone.utc))
        # Trusted test injection only; never controlled by a tool/webhook argument.
        self.phase_hook = phase_hook or (lambda phase, key: None)
        self.expense_label = expense_label  # Trusted destination label, never a model argument.

    def handle(self, event: dict) -> Result:
        """Only call after transport signature verification. Auth is checked again here."""
        source = event.get("source")
        if not isinstance(source, dict) or source.get("type") != "user" or source.get("userId") != self.settings.owner_id:
            return Result("denied", "")
        message = event.get("message")
        postback = (getattr(self, "supports_postback", False) and event.get("type") == "postback"
                    and isinstance(event.get("postback"), dict))
        if (not postback and (event.get("type") != "message" or not isinstance(message, dict) or message.get("type") != "text")) or event.get("mode", "active") != "active":
            return Result("ignored", "")
        event_id, stamp = event.get("webhookEventId"), event.get("timestamp")
        text = event["postback"].get("data") if postback else message.get("text")
        if (not isinstance(event_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", event_id)
                or type(stamp) is not int or stamp < 0
                or not isinstance(text, str) or not text.strip() or len(text) > 2000):
            return Result("invalid", "รูปแบบข้อความไม่ถูกต้อง")
        fingerprint = event_fingerprint(event)
        with self.state.session():
            now = self.clock()
            self.state.expire(now)
            if not self._prepare_context(event_id, fingerprint):
                return Result("ignored", "")
            previous = self.state.receipts.get(event_id)
            claim = self.state.claims.get(event_id)
            if ((previous and previous.fingerprint != fingerprint)
                    or (claim and claim.fingerprint != fingerprint)):
                return Result("conflict", "รหัสข้อความซ้ำแต่เนื้อหาเปลี่ยน จึงไม่ดำเนินการ")
            self._active_event_id = event_id  # Access only under the state session lock.
            self._active_event_kind = "postback" if postback else "text"
            if claim and claim.action_key in self.state.actions:
                result = self._resume(claim.action_key)
                result = self._finalize_result(event_id,result)
                self._receipt(event_id, fingerprint, result)
                return result
            if previous:
                from .action_trace import emit
                emit('receipt_replay')
                return deepcopy(previous.result)
            try:
                occurred_at = datetime.fromtimestamp(stamp / 1000, timezone.utc)
            except (ValueError, OverflowError, OSError):
                return Result("invalid", "เวลาในข้อความไม่ถูกต้อง")
            age = (now - occurred_at).total_seconds()
            if age > self.settings.max_new_event_age_seconds or age < -self.settings.max_future_skew_seconds:
                return Result("stale", "ข้อความอยู่นอกช่วงเวลาที่รับได้ กรุณาส่งคำสั่งใหม่")
            self.state.claims[event_id] = Claim(fingerprint, now + timedelta(seconds=self.settings.webhook_ttl_seconds))
            self._bind_context(event_id)
            self.state.checkpoint()
            # Restore proposals on infrastructure errors; keep attempted-effect markers.
            pending_before, last_before = deepcopy(self.state.pending), self.state.last
            try:
                result = self._route(text.strip(), event_id, stamp, occurred_at)
            except (ValidationError, ValueError):
                self.state.pending, self.state.last = pending_before, last_before
                result = Result("invalid", "ข้อมูลไม่ครบหรือไม่ถูกต้อง กรุณาตรวจสอบจำนวนเงินและวันเวลา")
            except (Conflict, NotFound) as error:
                self.state.pending, self.state.last = pending_before, last_before
                result = Result("conflict" if isinstance(error, Conflict) else "not_found",
                                "รายการเปลี่ยนหรือไม่พบ กรุณาตรวจสอบแล้วส่งคำสั่งใหม่")
            except Exception:
                claimed = self.state.actions.get(self.state.claims[event_id].action_key)
                # A failure saving/returning an already completed action must not
                # resurrect its consumed proposal or roll back its last reference.
                if not claimed or claimed.status != "completed":
                    self.state.pending, self.state.last = pending_before, last_before
                self.state.checkpoint()
                raise
            result = self._finalize_result(event_id,result)
            self._receipt(event_id, fingerprint, result)
            return result

    def _finalize_result(self,event_id,result):
        return result

    def _prepare_context(self, event_id, fingerprint):
        return True

    def _bind_context(self, event_id):
        pass

    def _receipt(self, event_id: str, fingerprint: str, result: Result):
        self.state.receipts[event_id] = Receipt(fingerprint, deepcopy(result),
                                               self.clock() + timedelta(seconds=self.settings.webhook_ttl_seconds))
        self.state.checkpoint()
        self.phase_hook("receipt_completed", event_id)

    def _blocked_action(self) -> str | None:
        return next((key for key, action in self.state.actions.items() if action.status != "completed"), None)

    @staticmethod
    def _uncertain(key: str) -> Result:
        return Result("uncertain", "ยังตรวจสอบผลรายการเดิมไม่ได้ จึงหยุดการเขียนเพิ่มไว้ กรุณาตรวจสอบผลก่อน",
                      {"action_ref": key, "needs_reconciliation": True})

    def _route(self, text: str, event_id: str, stamp: int, occurred_at: datetime) -> Result:
        pending = self.state.pending
        blocked = self._blocked_action()
        if re.fullmatch(r"ยกเลิก(?:รายการ(?:นี้|ที่รอ)?)?(?:ครับ|ค่ะ)?|cancel", text):
            if blocked:
                return self._uncertain(blocked)
            if pending and stamp <= pending.source_timestamp:
                return Result("stale", "คำตอบนี้เกิดก่อนคำถามล่าสุด จึงไม่ยกเลิกรายการ")
            self.state.pending = None
            return Result("cancelled", "ยกเลิกรายการที่รอแล้ว" if pending else "ไม่มีรายการที่รออยู่")
        read = read_request(text, occurred_at)
        if isinstance(read, PlannerNotice):
            return Result(read.status, read.message)
        guard = intent_guard(text) if read is None else None
        if guard:
            if blocked:
                return self._uncertain(blocked)
            if pending and stamp <= pending.source_timestamp:
                return Result("stale", "คำตอบนี้เกิดก่อนคำถามล่าสุด")
            if guard == "negated":
                self.state.pending = None
            return Result("no_action", "ไม่มีการบันทึกข้อความนี้" + (" และยกเลิกรายการที่รอแล้ว" if pending and guard == "negated" else ""))
        is_control = re.match(r"^(?:ยืนยัน|confirm|เลือก|ใหม่|เวลา)(?:\s|$)", text)
        if is_control:
            if blocked:
                return self._uncertain(blocked)
            if not pending:
                return Result("no_pending", "ไม่มีรายการที่รอยืนยัน หรือรายการหมดอายุแล้ว")
            if stamp <= pending.source_timestamp:
                return Result("stale", "คำตอบนี้เกิดก่อนคำถามล่าสุด กรุณาตอบคำถามล่าสุด")
            return self._answer(text, pending, event_id, stamp)
        if pending and read is None and not blocked:
            if stamp <= pending.source_timestamp:
                return Result("stale", "คำตอบนี้เกิดก่อนคำถามล่าสุด กรุณาตอบคำถามล่าสุด")
            if pending.kind in {"expense_draft", "expense_preview"}:
                return self._draft_answer(text, pending, stamp, occurred_at)
            if pending.kind in {"item", "new_item"}:
                self._refresh_catalog()
                answer = re.sub(r"^(?:หมายถึง|คือ|เป็น|รายการ)\s*", "", text)
                answer = re.sub(r"\s*(?:ครับ|ค่ะ|คะ|นะ)$", "", answer).strip()
                if self.catalog.match(answer).status == "exact":
                    return self._answer(f"เลือก {pending.token} {answer}", pending, event_id, stamp)
                if re.match(r"^(?:แก้|ยอด|จำนวน|วันที่)", text):
                    pending.kind = "expense_draft"
                    pending.payload = {"version": 1, "entries": pending.payload["entries"],
                                       "index": pending.payload["index"], "raw_items": pending.payload["raw_items"],
                                       "context_date": pending.payload["entries"][0]["date"]}
                    return self._draft_answer(text, pending, stamp, occurred_at)
        if not pending and not blocked and re.match(r"^แก้(?:ไข)?(?:ยอด|จำนวนเงิน|วันที่|รายการ)", text):
            return Result("no_pending", "ไม่มีรายการที่รอแก้ไข การแก้รายจ่ายที่บันทึกแล้วต้องตรวจสอบในชีต")
        planned = read if read is not None else self.planner.plan(text, occurred_at)
        if blocked and not (isinstance(planned, ToolRequest) and planned.name in {"list_events", "spending_summary"}):
            return self._uncertain(blocked)
        if pending and not (isinstance(planned, ToolRequest) and planned.name in {"list_events", "spending_summary"}):
            return Result("clarification", "มีรายการที่รออยู่ กรุณาตอบคำถามล่าสุด หรือพิมพ์ ยกเลิก",
                          {"token": pending.token})
        if isinstance(planned, PlannerNotice):
            return Result(planned.status, planned.message)
        if isinstance(planned, ExpenseDraft):
            draft = self._pending("expense_draft", event_id, stamp,
                                  {"version": 1, "entries": deepcopy(planned.entries), "index": 0,
                                   "raw_items": [e["item"] or "" for e in planned.entries],
                                   "context_date": planned.context_date})
            return self._draft_question(draft)
        if isinstance(planned, ExpensePreview):
            args = planned.request.validated_arguments()
            draft = self._pending("expense_preview", event_id, stamp,
                                  {"version": 1, "entries": args.model_dump(mode="json")["entries"], "index": 0,
                                   "raw_items": [e.item for e in args.entries],
                                   "context_date": occurred_at.date().isoformat()})
            return self._draft_question(draft)
        if isinstance(planned, CalendarClarification):
            if not planned.title or len(planned.title) > 200:
                raise ValueError("Invalid title")
            pending = self._pending("calendar_time", event_id, stamp, {"title": planned.title})
            return Result("clarification", f"ต้องการนัดวันเวลาใด? พิมพ์ เวลา {pending.token} | YYYY-MM-DD HH:MM | YYYY-MM-DD HH:MM",
                          {"token": pending.token, "stage": pending.kind})
        if planned is None:
            return Result("help", "ลอง BTS 65, ตารางวันนี้ หรือ นัด ชื่อ | YYYY-MM-DD HH:MM | YYYY-MM-DD HH:MM")
        # Revalidate at the server boundary even if a future planner returns model output.
        request = ToolRequest.model_validate(planned.model_dump())
        args = request.validated_arguments()
        key = self._key(event_id, request.name)
        if isinstance(args, AddTransactions):
            return self._transactions(args, key, event_id, stamp)
        if isinstance(args, SpendingSummary):
            totals = self.sheets.summary(args.start_date, args.end_date)
            return Result("summary", f"รายจ่าย {totals['expense']} บาท ({self.expense_label})", totals)
        if isinstance(args, ListEvents):
            events = self.calendar.list(args.start, args.end)
            shown = events[:10]
            # as_dict normalizes Bangkok; external titles are plain data, never instructions.
            data = [event.as_dict() for event in shown]
            lines = [f"{event['start']} {event['title']}" for event in data]
            extra = len(events) - len(shown)
            message = "\n".join(lines) or "วันนี้ไม่มีนัด (ข้อมูลจำลอง)"
            if extra:
                message += f"\nและอีก {extra} รายการ"
            return Result("agenda", message, {"events": data, "overflow": extra})
        if isinstance(args, CreateEvent):
            return self._create(args, key, event_id, stamp)
        if isinstance(args, (UpdateEvent, DeleteEvent)):
            target_id = self._event_ref(args.event_ref)
            target = self.calendar.get(target_id)
            payload = {"target_id": target_id, "version": target.version, "target": target.as_dict(),
                       "arguments": args.model_dump(mode="json")}
            kind = "update" if isinstance(args, UpdateEvent) else "delete"
            if kind == "update" and args.start is not None:
                payload["conflicts"] = [event.as_dict() for event in self.calendar.list(args.start, args.end)
                                        if event.id != target_id][:10]
            pending = self._pending(kind, event_id, stamp, payload)
            verb = "แก้ไข" if kind == "update" else "ลบ"
            warning = " มีนัดทับซ้อนกับเวลาใหม่" if payload.get("conflicts") else ""
            return Result("clarification", f"{verb}นัด {target.title} ({target.as_dict()['start']})?{warning} พิมพ์ ยืนยัน {pending.token} หรือ ยกเลิก",
                          {"token": pending.token, "stage": kind, **payload})
        raise ValueError("Unsupported tool")

    def _key(self, origin_id: str, name: str) -> str:
        return f"{self.settings.owner_id}:{origin_id}:{name}"

    def _refresh_catalog(self):
        """Production overrides this read, only inside authenticated owner routing."""
        return self.catalog

    @staticmethod
    def _draft_entries(pending):
        payload = pending.payload
        if (set(payload) != {"version", "entries", "index", "raw_items", "context_date"}
                or payload["version"] != 1 or not isinstance(payload["entries"], list)
                or not 1 <= len(payload["entries"]) <= 5 or type(payload["index"]) is not int
                or not 0 <= payload["index"] < len(payload["entries"])
                or not isinstance(payload["raw_items"], list) or len(payload["raw_items"]) != len(payload["entries"])
                or any(not isinstance(v, str) or len(v) > 200 for v in payload["raw_items"])):
            raise ValueError("Invalid bounded expense draft")
        date.fromisoformat(payload["context_date"])
        entries = deepcopy(payload["entries"])
        for entry in entries:
            if set(entry) != {"item", "amount", "kind", "date", "note"} or entry["kind"] != "expense":
                raise ValueError("Draft fields are server-owned and bounded")
            # Validate every populated field with the existing contracts.
            check = {**entry, "item": entry["item"] or "draft", "amount": entry["amount"] or "1",
                     "date": entry["date"] or payload["context_date"]}
            AddTransactions.model_validate({"entries": [check]})
        return entries

    def _draft_question(self, pending):
        entries = self._draft_entries(pending)
        missing = sorted({field for e in entries for field in ("item", "amount", "date") if e[field] is None})
        data = {"token": pending.token, "stage": pending.kind, "missing": missing, "entries": entries}
        if missing:
            labels = {"item": "ชื่อรายการ", "amount": "จำนวนเงินที่เป็นบวก", "date": "วันที่พร้อมเดือน (เช่น 11 ตค 2026)"}
            return Result("clarification", "ขอ " + ", ".join(labels[k] for k in missing)
                          + " สำหรับรายการที่รอ หรือพิมพ์ ยกเลิก", data)
        pending.kind = "expense_preview"
        data["stage"] = pending.kind
        lines = [f"{e['item']} {e['amount']} บาท วันที่ {e['date']}" for e in entries]
        return Result("clarification", "ตรวจสอบรายการ:\n" + "\n".join(lines)
                      + f"\nพิมพ์ ยืนยัน {pending.token} เพื่อบันทึก หรือ ยกเลิก", data)

    def _draft_answer(self, text, pending, stamp, occurred_at):
        entries = self._draft_entries(pending)
        if len(entries) > 1 and any(e[k] is None for e in entries for k in ("item", "amount", "date")):
            return Result("clarification", "มีหลายรายการที่ข้อมูลไม่ครบ กรุณายกเลิกแล้วส่งแต่ละรายการให้ครบ", {"token": pending.token})
        self._refresh_catalog()
        clean = re.sub(r"^แก้(?:ไข)?(?:ยอด|จำนวนเงิน|วันที่|รายการ)?(?:เป็น)?\s*", "", text)
        ds = dates(clean, occurred_at)
        names = item_spans(clean, self.catalog)
        ms = amounts(clean, (*ds.spans, *((a,b) for a,b,_ in names)))
        target = entries[pending.payload["index"]]
        raw_item = None
        if target["item"] is None and not names and not ms and not ds.explicit:
            raw_item = re.sub(r"^(?:หมายถึง|คือ|เป็น|รายการ)\s*", "", clean)
            raw_item = re.sub(r"\s*(?:ครับ|ค่ะ|คะ|นะ)$", "", raw_item).strip()
            if not 1 <= len(raw_item) <= 200 or re.search(r"[\n\d?]|สรุป|วันที่|นัด|ตาราง", raw_item):
                raw_item = None
        # Only exact small field replies/corrections may update a persisted draft.
        masked = list(clean)
        for a,b in (*ds.spans, *((a.start,a.end) for a in ms), *((a,b) for a,b,_ in names)):
            masked[a:b] = " " * (b-a)
        residual = "".join(masked)
        if (len(names) > 1 or len(ms) > 1 or (ms and ms[0].value is None)
                or (ds.explicit and ds.value is None)
                or (raw_item is None and not re.fullmatch(r"(?:\s|ยอด|จำนวนเงิน|จำนวน|เงิน|บาท|วันที่|เป็น|หมายถึง|คือ|รายการ|ครับ|ค่ะ|คะ|นะ)*", residual))):
            return self._draft_question(pending)
        entry = entries[pending.payload["index"]]
        changed = False
        if names:
            entry["item"], changed = names[0][2], True
            if not pending.payload["raw_items"][pending.payload["index"]]:
                pending.payload["raw_items"][pending.payload["index"]] = entry["item"]
        elif raw_item:
            entry["item"], changed = raw_item, True
            pending.payload["raw_items"][pending.payload["index"]] = raw_item
        if ms:
            entry["amount"], changed = ms[0].value, True
        if ds.explicit:
            entry["date"], changed = ds.value.isoformat(), True
        if not changed:
            return self._draft_question(pending)
        # Keep original origin/key/TTL; rotate token so an old preview cannot
        # confirm a correction. This stores fields only, never full messages.
        pending.payload["entries"] = entries
        pending.source_timestamp, pending.token = stamp, secrets.token_hex(4)
        return self._draft_question(pending)

    def _pending(self, kind: str, origin_id: str, stamp: int, payload: dict) -> Pending:
        expiry = self.clock() + timedelta(seconds=self.settings.pending_ttl_seconds)
        previous = self.state.pending
        if previous and previous.origin_event_id == origin_id:
            expiry = min(expiry, previous.expires_at)
        pending = Pending(kind, secrets.token_hex(4), origin_id, stamp,
                          expiry, payload)
        self.state.pending = pending
        return pending

    def _execute(self, key: str, kind: str, payload: dict, origin_id: str, metadata: dict | None = None) -> Result:
        existing = self.state.actions.get(key)
        if existing and (existing.kind != kind or existing.payload != payload):
            raise Conflict("Prepared action changed")
        if not existing:
            now = self.clock()
            deadline = self.state.pending.expires_at if self.state.pending else now + timedelta(seconds=self.settings.pending_ttl_seconds)
            details = {**(metadata or {}), "origin_id": origin_id,
                       "last_expires_at": (now + timedelta(seconds=self.settings.last_action_ttl_seconds)).isoformat()}
            self.state.actions[key] = Action(kind, deepcopy(payload), deepcopy(details), deadline,
                                             now + timedelta(seconds=self.settings.webhook_ttl_seconds))
        self.state.claims[self._active_event_id].action_key = key
        self.state.checkpoint()
        self.phase_hook("prepared", key)
        return self._resume(key)

    def _resume(self, key: str) -> Result:
        action = self.state.actions[key]
        if action.status == "completed":
            return deepcopy(action.result)
        provider = self.sheets if action.kind in {"add_transactions", "catalog_expense"} else self.calendar
        if action.status in {"executing", "uncertain"}:
            try:
                proof = (provider.reconcile_enrollment(key, action.payload) if action.kind == "catalog_expense"
                         else provider.reconcile(key, action.payload))
            except Exception:
                proof = None
            if proof and proof.outcome == "committed":
                return self._complete(key, proof.receipt)
            if not proof or proof.outcome != "not_committed":
                action.status = "uncertain"
                self.state.checkpoint()
                return self._uncertain(key)
            action.status = "prepared"
            self.state.checkpoint()
        if action.deadline <= self.clock():
            action.status = "completed"
            action.result = Result("expired", "รายการที่ยังไม่ได้เขียนหมดอายุแล้ว กรุณาส่งคำสั่งใหม่")
            self.state.checkpoint()
            return deepcopy(action.result)
        action.status = "executing"
        self.state.checkpoint()  # Must commit BEFORE any provider call.
        self.phase_hook("executing", key)
        self.state.before_effect()
        payload = action.payload
        try:
            if action.kind == "add_transactions":
                receipt = self.sheets.add(AddTransactions.model_validate(payload).entries, key)
            elif action.kind == "catalog_expense":
                receipt = self.sheets.enroll_and_add(payload, key)
            elif action.kind == "create_event":
                from .calendar_event_types import parse_payload
                args=parse_payload(payload) if action.metadata.get('calendar_mvp') else CreateEvent.model_validate(payload)
                receipt = self.calendar.create(args, key)
            elif action.kind == "update_event":
                receipt = self.calendar.update(payload["id"], UpdateEvent.model_validate(payload["patch"]), payload["version"], key)
            elif action.kind == "delete_event":
                receipt = self.calendar.delete(payload["id"], payload["version"], key)
            else:
                raise ValueError("Unsupported stored action")
        except WriteBlocked:
            action.status = "completed"
            action.result = Result("setup_required", "ยังไม่ได้ตั้งค่าปลายทางหรือสิทธิ์สำหรับรายการนี้ จึงยังไม่บันทึก")
            self.state.checkpoint()
            return deepcopy(action.result)
        except (Conflict, NotFound) as error:
            # Adapter contract: these errors guarantee no write took effect.
            action.status = "completed"
            action.result = Result("conflict" if isinstance(error, Conflict) else "not_found",
                                   "รายการเปลี่ยนหรือไม่พบ กรุณาตรวจสอบแล้วส่งคำสั่งใหม่")
            self.state.checkpoint()
            return deepcopy(action.result)
        except Exception as error:
            action.status = "uncertain"
            self.state.checkpoint()
            raise ProviderFailure("Provider outcome requires reconciliation") from error
        self.phase_hook("effect_returned", key)
        return self._complete(key, receipt)

    def _complete(self, key: str, receipt: object) -> Result:
        action = self.state.actions[key]
        if action.kind in {"add_transactions", "catalog_expense"}:
            refs = tuple(receipt)
            from .reply_display import expense_lines
            details = expense_lines(action.payload['entries'])
            result = Result("recorded", f"บันทึกรายจ่าย{self.expense_label} {len(refs)} รายการแล้วครับ\n{details}",
                            {"refs": refs, "items": [entry["item"] for entry in action.payload["entries"]],
                             "raw_items": action.metadata["raw_items"],
                             "entries": [{k:deepcopy(entry[k]) for k in ('item','amount','date','note') if k in entry}
                                         for entry in action.payload['entries']]})
            last_kind = "transactions"
        elif action.kind in {"create_event", "update_event"}:
            refs, last_kind = (receipt.id,), "event"
            status, verb = ("created", "สร้าง") if action.kind == "create_event" else ("updated", "แก้ไข")
            result = Result(status, f"{verb}นัดจำลอง {receipt.title} แล้ว", receipt.as_dict())
        else:
            refs, last_kind = (), None
            result = Result("deleted", "ลบนัดจำลองแล้ว", {"id": receipt})
        if self.state.pending and self.state.pending.origin_event_id == action.metadata["origin_id"]:
            self.state.pending = None
        self.state.last = LastAction(last_kind, refs, datetime.fromisoformat(action.metadata["last_expires_at"])) if last_kind else None
        action.status, action.result = "completed", deepcopy(result)
        self.state.expire(self.clock())
        self.state.checkpoint()  # Action result, pending consumption and last ref commit together.
        self.phase_hook("action_completed", key)
        return result

    def _event_ref(self, value: str) -> str:
        if value in {"last", "ล่าสุด"}:
            if not self.state.last or self.state.last.kind != "event":
                raise NotFound("No unexpired last Calendar action")
            return self.state.last.refs[0]
        return value

    def _transactions(self, args: AddTransactions, key: str, origin_id: str, stamp: int,
                      raw_items: list[str] | None = None) -> Result:
        if any(entry.kind == "income" for entry in args.entries):
            return Result("income_unconfigured", "ยังไม่ได้ตั้งค่าตารางรายรับ จึงยังไม่บันทึกรายการนี้")
        raw_items = raw_items or [entry.item for entry in args.entries]
        resolved = []
        for index, entry in enumerate(args.entries):
            match = self.catalog.match(entry.item)
            if match.canonical is None:
                pending = self._pending("item", origin_id, stamp, {
                    "entries": args.model_dump(mode="json")["entries"], "index": index,
                    "raw_items": raw_items,
                })
                candidates = ", ".join(match.suggestions)
                message = f"ไม่พบชื่อที่ตรงกับ {entry.item}"
                if candidates:
                    message += f" อาจหมายถึง {candidates}"
                message += f". พิมพ์ เลือก {pending.token} ชื่อจาก Database, ใหม่ {pending.token} หรือ ยกเลิก"
                return Result("clarification", message, {"token": pending.token, "stage": "item",
                                                         "match": match.status, "suggestions": list(match.suggestions)})
            resolved.append(entry.model_copy(update={"item": match.canonical}))
        payload = {"entries": [entry.model_dump(mode="json") for entry in resolved]}
        return self._execute(key, "add_transactions", payload, origin_id, {"raw_items": raw_items})

    def _create(self, args: CreateEvent, key: str, origin_id: str, stamp: int, approved: bool = False) -> Result:
        if not approved and key not in self.state.actions:
            conflicts = self.calendar.list(args.start, args.end)
            if conflicts:
                pending = self._pending("calendar_conflict", origin_id, stamp,
                                        {"arguments": args.model_dump(mode="json")})
                return Result("clarification", f"นัดนี้ทับกับนัดเดิม ต้องการสร้างต่อหรือไม่? พิมพ์ ยืนยัน {pending.token} หรือ ยกเลิก",
                              {"token": pending.token, "stage": pending.kind,
                               "conflicts": [event.as_dict() for event in conflicts[:10]]})
        return self._execute(key, "create_event", args.model_dump(mode="json"), origin_id)

    def _answer(self, text: str, pending: Pending, answer_id: str, stamp: int) -> Result:
        choose = re.fullmatch(r"เลือก\s+([a-f0-9]{8})\s+(.+)", text)
        if choose and pending.kind in {"item", "new_item"} and choose[1] == pending.token:
            self._refresh_catalog()  # Refresh before matching on a cold process.
            match = self.catalog.match(choose[2])
            if match.canonical is None or match.status != "exact":
                return Result("clarification", "กรุณาเลือกชื่อที่มีอยู่ใน Database", {"token": pending.token})
            entries = deepcopy(pending.payload["entries"])
            entries[pending.payload["index"]]["item"] = match.canonical
            args = AddTransactions.model_validate({"entries": entries})
            return self._transactions(args, self._key(pending.origin_event_id, "add_transactions"),
                                      pending.origin_event_id, stamp, pending.payload["raw_items"])
        if text == f"ใหม่ {pending.token}" and pending.kind == "item":
            pending.kind = "new_item"
            pending.source_timestamp = stamp
            return Result("new_item_unconfigured", "รายการใหม่นี้ยังบันทึกไม่ได้ กรุณาเลือกชื่อจาก Database หรือ ยกเลิก",
                          {"token": pending.token, "stage": pending.kind})
        parts = [part.strip() for part in text.split("|")]
        if parts[0] == f"เวลา {pending.token}" and pending.kind == "calendar_time":
            request = RulePlanner.calendar_request([pending.payload["title"], *parts[1:]])
            args = request.validated_arguments()
            return self._create(args, self._key(pending.origin_event_id, "create_event"),
                                pending.origin_event_id, stamp)
        if text not in {f"ยืนยัน {pending.token}", f"confirm {pending.token}"}:
            return Result("clarification", "คำตอบไม่ตรงกับรายการที่รอ กรุณาใช้รหัสล่าสุดหรือ ยกเลิก",
                          {"token": pending.token})
        if pending.kind == "expense_preview":
            entries = self._draft_entries(pending)
            args = AddTransactions.model_validate({"entries": entries})
            return self._transactions(args, self._key(pending.origin_event_id, "add_transactions"),
                                      pending.origin_event_id, stamp, pending.payload["raw_items"])
        if pending.kind == "calendar_conflict":
            args = CreateEvent.model_validate(pending.payload["arguments"])
            return self._create(args, self._key(pending.origin_event_id, "create_event"),
                                pending.origin_event_id, stamp, approved=True)
        if pending.kind == "delete":
            payload = {"id": pending.payload["target_id"], "version": pending.payload["version"]}
            key = self._key(pending.origin_event_id, "delete_event")
            return self._execute(key, "delete_event", payload, pending.origin_event_id)
        if pending.kind == "update":
            args = UpdateEvent.model_validate(pending.payload["arguments"])
            key = self._key(pending.origin_event_id, "update_event")
            payload = {"id": pending.payload["target_id"], "version": pending.payload["version"],
                       "patch": args.model_dump(mode="json")}
            return self._execute(key, "update_event", payload, pending.origin_event_id)
        return Result("clarification", "รายการนี้ต้องเลือกชื่อหรือระบุวันเวลาก่อน", {"token": pending.token})
