"""Temporary authorized production startup check; zero provider requests."""

import json
import os
import sys

from fastapi.testclient import TestClient
import httpx

from line_assistant.cloud_run import create_cloud_run_app
from line_assistant.main import signature
from line_assistant.pilot_config import PILOT_KEYS, PilotConfig
from line_assistant.runtime_config import ConfigError


def run_checks(values, *, secret_loader=None):
    config = PilotConfig.from_mapping(values)
    if (not config.launch_authorized or config.mode != "production" or config.monthly_micro_usd != 500000
            or config.max_calls_day not in {20, 100} or config.max_calls_month != 300):
        raise ConfigError("Explicit approved production configuration required")
    requests = []

    def deny(request):
        requests.append(1)
        raise RuntimeError("Startup check cannot access providers")

    app = create_cloud_run_app(values, transport=httpx.MockTransport(deny), secret_loader=secret_loader)
    channel_secret = app.state.assistant.settings.channel_secret
    other_owner = "U" + ("b" if config.owner_id == "U" + "a" * 32 else "a") * 32
    with TestClient(app) as client:
        assert client.get("/health").json() == {"status": "ok", "mode": "owner_expenses"}
        body = b'{"events":[]}'
        valid = client.post("/webhooks/line", content=body, headers={"x-line-signature": signature(body, channel_secret)})
        invalid = client.post("/webhooks/line", content=body, headers={"x-line-signature": "invalid"})
        event = {"type": "message", "webhookEventId": "startup-denied", "timestamp": 0,
                 "source": {"type": "user", "userId": other_owner},
                 "message": {"type": "text", "id": "startup-denied", "text": "startup check"}}
        denied_body = json.dumps({"events": [event]}, separators=(",", ":")).encode()
        denied = client.post("/webhooks/line", content=denied_body,
                             headers={"x-line-signature": signature(denied_body, channel_secret)})
        assert valid.status_code == 200 and valid.json() == {"ok": True}
        assert invalid.status_code == 401 and denied.status_code == 200
        assert requests == []
    return {"status": "passed", "signed_empty_http_status": 200, "invalid_signature_http_status": 401,
            "nonowner_ignored": True, "provider_requests": 0}


def main():
    try:
        values = {key: os.environ[key] for key in PILOT_KEYS if key in os.environ}
        result = run_checks(values)
    except Exception:
        print('production_signed_check_result {"status":"failed"}', flush=True)
        raise SystemExit(1) from None
    print("production_signed_check_result " + json.dumps(result, separators=(",", ":")), flush=True)
    # Normal reviewed server entry, with a fresh live app after the isolated check closes.
    from container_entrypoint import main as serve
    serve()


if __name__ == "__main__":
    main()
