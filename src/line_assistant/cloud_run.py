"""Prepared expenses-only Cloud Run entry point. Launch defaults to disabled.

Reads only explicit nonsecret PILOT env keys. Mounted secrets/metadata/paid network
are consulted only after a separately authorized, complete future launch config.
No existing .env, ADC/user key or unrelated project credential lookup.
"""
from .expense_storage import storage_scope

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from pydantic import SecretStr

from .catalog import ItemCatalog
from .canary import CanaryGate, CanaryPlanner
from .config import Settings
from .firestore_state import FirestoreState
from .line_api import LineReplyApi, ReplyDispatcher
from .main import create_app
from .model_api import DurableModelGuard, ResponsesPlanner
from .pilot_config import PILOT_KEYS, SECRET_PATHS, PilotConfig
from .pilot_http import PilotHttp, PilotPort, rpc_budget
from .readonly_preflight import TEST_WORKBOOK_ID
from .runtime_config import ConfigError
from .reply_check import ReplyCheckAssistant, ReplyCheckGate, ReplyCheckReplies
from .service import Assistant
from .state import Result
from .sheets_api import SheetsApi
from .transport import NotReady, WriteBlocked


def read_mounted_secret(name):
    # Future authorized container only; these are fixed Secret Manager mount paths.
    with Path(SECRET_PATHS[name]).open("rb") as file:
        value = file.read(8193).decode("ascii")
    if not 1 <= len(value) <= 8192 or any(not 33 <= ord(c) <= 126 for c in value):
        raise NotReady("Invalid mounted secret; never put its value in diagnostics")
    return SecretStr(value)


class DisabledCalendar:
    def __getattr__(self, name):
        def disabled(*args, **kwargs):
            raise WriteBlocked("Calendar is outside this pilot")
        return disabled


class ExpenseAssistant(Assistant):
    def _prepare_context(self, event_id, fingerprint):
        # Called inside the existing fenced owner session, before replay/resume.
        target = storage_scope(self.sheets)
        bound = self.state.event_workbooks.get(event_id)
        if bound and bound != target:
            return False
        if self.production and not self.state.production_ready:
            return False
        http = getattr(getattr(self.sheets, "client", None), "http", None)
        legacy = getattr(getattr(http, "config", None), "legacy_spreadsheet_id", TEST_WORKBOOK_ID) or target
        self.state.select_workbook(target, legacy_workbook=legacy)
        return self.state.event_workbooks.get(event_id, target) == target

    def _bind_context(self, event_id):
        self.state.event_workbooks[event_id] = storage_scope(self.sheets)

    def handle(self, event):
        with rpc_budget(96):
            return super().handle(event)

    def _transactions(self, *args, **kwargs):
        # Read canonical master names only after signature/owner/plan validation.
        self._refresh_catalog()
        return super()._transactions(*args, **kwargs)

    def _refresh_catalog(self):
        self.state.before_effect()
        self.catalog = self.sheets.catalog()
        return self.catalog


class BoundedReplies(ReplyDispatcher):
    def deliver(self, *args):
        with rpc_budget(24, seconds=10):
            result = super().deliver(*args)
            if result == "sent" and getattr(self.assistant, "canary", None):
                with self.assistant.state.session():
                    state = self.assistant.state
                    run = state.canary
                    receipt = state.receipts.get(run.event_id) if run else None
                    action = state.actions.get(self.assistant._key(run.event_id, "add_transactions")) if run else None
                    reply = state.replies.get(run.event_id) if run else None
                    if (run and all((run.model_attempted, run.append_attempted, run.reply_attempted))
                            and receipt and receipt.result.status == "recorded" and len(receipt.result.data.get("refs", ())) == 1
                            and action and action.status == "completed" and action.result == receipt.result
                            and reply and reply.status == "sent"):
                        state.production_ready = True
                        state.checkpoint()
            return result


class CanaryAssistant(ExpenseAssistant):
    def handle(self, event):
        if not self.canary.accepts(event):
            return Result("ignored", "")
        with rpc_budget(96):
            with self.state.session():
                if not self.canary.bind(event):
                    self.canary.log_diagnostic(event, "binding_mismatch")
                    return Result("ignored", "")
                self.phase_hook("canary_bound", event["webhookEventId"])
            return Assistant.handle(self, event)

    def _transactions(self, args, *args_rest, **kwargs):
        self.canary.current()
        if not self.canary.spec.matches_entries(args):
            return Result("canary_blocked", "")
        return super()._transactions(args, *args_rest, **kwargs)


class CanaryReplies(BoundedReplies):
    def deliver(self, event, result, received_at):
        if result.status != "recorded" or not self.assistant.canary.accepts(event):
            return "skipped"
        return super().deliver(event, result, received_at)


def create_cloud_run_app(values=None, *, transport=None, secret_loader=None, clock=None):
    config = PilotConfig.from_mapping(values or {})
    if config.expense_backend != "sheets":
        raise ConfigError("Firestore expenses require the history production factory")
    if transport is None and config.launch_authorized and config.mode == "canary" and config.canary is None:
        raise ConfigError("Live transport requires the reviewed canary configuration")
    if transport is None and (config.launch_authorized or config.auth_check_id or config.reply_check and config.reply_check.authorized):
        config.require_live_target()
    if config.domain_tool_loop_enabled:
        import httpx
        if not isinstance(transport, httpx.MockTransport):
            raise ConfigError('Domain-tool candidate is mock-only; live execution is not authorized')
    if config.reply_check and config.reply_check.authorized:
        loader = secret_loader or read_mounted_secret
        try:
            secrets = {name: loader(name) for name in ("line-channel-secret", "line-channel-access-token")}
            if any(type(value) is not SecretStr or not 1 <= len(value.get_secret_value()) <= 8192
                   or any(not 33 <= ord(c) <= 126 for c in value.get_secret_value()) for value in secrets.values()):
                raise ValueError
            http = PilotHttp(config, secrets, transport=transport, clock=clock)
            settings = Settings(owner_id=config.owner_id, channel_secret=secrets["line-channel-secret"].get_secret_value(),
                                mode="synthetic_pilot")
            state = FirestoreState(PilotPort(http, "firestore"), config.reply_check.state_owner(config.owner_id),
                                   project_id=config.project_id, collection_id="assistant_pilot_states", clock=clock,
                                   max_snapshot_bytes=200000)
            http.state = state
            gate = ReplyCheckGate(config.reply_check, state, config.owner_id, http.clock)
            http.reply_check = gate
            assistant = ReplyCheckAssistant(settings, state, gate, http.clock)
            replies = ReplyCheckReplies(LineReplyApi(PilotPort(http, "line")), assistant)
            app = create_app(assistant, replies, inspection_mode=False, event_limit=5, body_limit=16384,
                             app_mode="reply_check", verified_events_observer=gate.observe_verified_events)
            @asynccontextmanager
            async def lifespan(app):
                yield
                http.close()
            app.router.lifespan_context = lifespan
            app.state.pilot_http, app.state.pilot_replies = http, replies
            return app
        except Exception:
            raise NotReady("Reply-check bootstrap incomplete; inspect setup without printing secrets") from None
    if not config.launch_authorized:
        if config.auth_check_id:
            from .line_auth_check import run_line_auth_check
            run_line_auth_check(config, secret_loader or read_mounted_secret, transport=transport, clock=clock)
        app = FastAPI(title="LINE assistant — launch disabled", docs_url=None, redoc_url=None, openapi_url=None)
        @app.get("/health")
        def health():
            return {"status": "ok", "mode": "launch_disabled", "live_requests_enabled": False}
        @app.post("/webhooks/line")
        def disabled():
            raise HTTPException(503, "Pilot launch is disabled")
        return app
    if config.mode == "canary" and config.canary is None and transport is None:
        raise ConfigError("Live launch requires the reviewed durable canary configuration")
    # This branch is prepared, not invoked with real secrets/transport in this task.
    loader = secret_loader or read_mounted_secret
    try:
        secrets = {name: loader(name) for name in SECRET_PATHS}
        if any(type(value) is not SecretStr or not 1 <= len(value.get_secret_value()) <= 8192
               or any(not 33 <= ord(c) <= 126 for c in value.get_secret_value()) for value in secrets.values()):
            raise ValueError
        http = PilotHttp(config, secrets, transport=transport, clock=clock)
        settings = Settings(owner_id=config.owner_id, channel_secret=secrets["line-channel-secret"].get_secret_value(),
                            mode="production" if config.mode == "production" else "synthetic_pilot")
        state = FirestoreState(PilotPort(http, "firestore"), config.owner_id, project_id=config.project_id,
                               collection_id="assistant_pilot_states", clock=clock, max_snapshot_bytes=800000 if config.state_codec_enabled else 200000,
                               compact_snapshots=config.state_codec_enabled)
        http.state = state
        target = config.sheet_target()
        sheets = SheetsApi(PilotPort(http, "sheets"), target, effect_guard=state.before_effect)
        assistant_type = CanaryAssistant if config.mode == "canary" and config.canary else ExpenseAssistant
        if config.conversation_enabled:
            from .conversation import ConversationAssistant
            assistant_type = ConversationAssistant
        if config.domain_tool_loop_enabled:
            from .domain_runtime import ToolLoopAssistant
            assistant_type = ToolLoopAssistant
        assistant = assistant_type(settings, sheets, DisabledCalendar(), ItemCatalog([], {}), state=state,
                                     clock=clock, expense_label="ในชีตจริง" if config.mode == "production" else "ในชีตทดสอบ",
                                     **({'candidate_enabled': True} if config.domain_tool_loop_enabled else {}))
        assistant.production = config.mode == "production"
        guard = DurableModelGuard(state, monthly_micro_usd=config.monthly_micro_usd,
                                  max_calls_day=config.max_calls_day, max_calls_month=config.max_calls_month, clock=clock)
        assistant.planner = ResponsesPlanner(PilotPort(http, "model"), guard, lambda: assistant._active_event_id,
                                            catalog_provider=assistant._refresh_catalog if config.mode == "production" else None)
        if config.conversation_enabled:
            from .intent_api import IntentPlanner
            assistant.planner = IntentPlanner(PilotPort(http, 'model'), guard, lambda: assistant._active_event_id)
        if config.domain_tool_loop_enabled:
            from .tool_loop import ToolLoop
            assistant.loop = ToolLoop(PilotPort(http, 'model'), guard)
        if config.mode == "canary" and config.canary:
            assistant.canary = http.canary = CanaryGate(config.canary, state, config.owner_id, assistant.clock)
            assistant.planner = CanaryPlanner(assistant.planner, config.canary)
        replies_type = CanaryReplies if config.mode == "canary" and config.canary else BoundedReplies
        replies = replies_type(LineReplyApi(PilotPort(http, "line")), assistant)
        app = create_app(assistant, replies, inspection_mode=False, event_limit=5, body_limit=16384,
                         app_mode="owner_expenses" if config.mode == "production" else "synthetic_canary",
                         verified_events_observer=assistant.canary.observe_verified_events
                         if isinstance(assistant, CanaryAssistant) else None)
        @asynccontextmanager
        async def lifespan(app):
            yield
            http.close()
        app.router.lifespan_context = lifespan
        app.state.pilot_http = http
        app.state.pilot_replies = replies
        return app
    except Exception:
        raise NotReady("Pilot bootstrap incomplete; inspect setup without printing secrets") from None


def app_factory():
    import os
    # Explicit nonsecret allowlist only; no dotenv or secret environment inspection.
    return create_cloud_run_app({key: os.environ[key] for key in PILOT_KEYS if key in os.environ})
