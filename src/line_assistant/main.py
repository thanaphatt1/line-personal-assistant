import base64
from dataclasses import asdict
import hashlib
import hmac
import json
from importlib.resources import files

from fastapi import FastAPI, HTTPException, Request
from starlette.concurrency import run_in_threadpool

from .adapters import MockCalendar, MockSheets, SheetLayout
from .catalog import ItemCatalog
from .config import Settings
from .service import Assistant
from .runtime_config import RuntimeConfig
from .transport import LiveDisabled


def signature(body: bytes, secret: str) -> str:
    return base64.b64encode(hmac.new(secret.encode(), body, hashlib.sha256).digest()).decode("ascii")


def mock_assistant() -> Assistant:
    fixture = json.loads(files("line_assistant").joinpath("data/mock_sheet.json").read_text(encoding="utf-8"))
    layout = SheetLayout(fixture["columns"], tuple(fixture["formula_columns"]), fixture["schema_verified"],
                         fixture.get("date_mode", "iso_mock"))
    return Assistant(Settings(), MockSheets(layout), MockCalendar(),
                     ItemCatalog(fixture["database_items"], fixture["aliases"]))


def create_app_from_config(config: RuntimeConfig) -> FastAPI:
    """Explicit local mock factory; staged private fields never enter mock state."""
    if config.mode != "mock" or config.environment != "local" or config.live_requests_enabled:
        raise LiveDisabled("Configured live/hosted startup is not implemented or enabled")
    return create_app()  # Public mock Settings only, no secret unwrapping/providers.


def create_app(assistant: Assistant | None = None, reply_dispatcher=None, *,
               inspection_mode=True, event_limit=50, body_limit=128 * 1024, app_mode="mock",
               verified_events_observer=None) -> FastAPI:
    assistant = assistant or mock_assistant()
    app = FastAPI(title="LINE assistant", docs_url=None, redoc_url=None, openapi_url=None)
    app.state.assistant = assistant

    @app.get("/health")
    def health():
        return {"status": "ok", "mode": app_mode}

    @app.post("/webhooks/line")
    async def webhook(request: Request):
        received_at = assistant.clock()
        chunks, size = [], 0
        async for chunk in request.stream():
            size += len(chunk)
            if size > body_limit:
                raise HTTPException(413, "Webhook body too large")
            chunks.append(chunk)
        body = b"".join(chunks)
        received_signature = request.headers.get("x-line-signature", "")
        expected = signature(body, assistant.settings.channel_secret)
        try:
            valid = hmac.compare_digest(expected, received_signature)
        except TypeError:
            valid = False
        if not valid:
            raise HTTPException(401, "Invalid LINE signature")
        # No JSON decoding, model planning, or account access before signature validation.
        try:
            envelope = json.loads(body)
        except (ValueError, UnicodeDecodeError):
            raise HTTPException(400, "Invalid JSON") from None
        events = envelope.get("events") if isinstance(envelope, dict) else None
        if not isinstance(events, list) or len(events) > event_limit or any(not isinstance(event, dict) for event in events):
            raise HTTPException(400, "Invalid events")
        if verified_events_observer:
            verified_events_observer(events)
        results, deliveries = [], []
        for event in events:
            try:
                result = await run_in_threadpool(assistant.handle, event)
                results.append(asdict(result))
                if reply_dispatcher:
                    deliveries.append(await run_in_threadpool(reply_dispatcher.deliver, event, result, received_at))
            except Exception:
                # Don't return adapter errors or log raw financial/calendar content.
                raise HTTPException(503, "Adapter failed; retry with the same webhook event") from None
        if not inspection_mode:
            return {"ok": True}
        output = {"mode": app_mode, "results": results}
        if reply_dispatcher:
            output["reply_delivery"] = deliveries
        return output

    return app


app = create_app()
