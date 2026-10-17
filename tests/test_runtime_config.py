from dataclasses import asdict
import json
from pathlib import Path
import traceback

from fastapi.testclient import TestClient
import pytest

from line_assistant.auth_ports import TokenProviders
from line_assistant.config import Settings
from line_assistant.main import create_app_from_config
from line_assistant.mock_api import MockApiServices
from line_assistant.runtime_config import ConfigError, ENV_KEYS, load_config, load_config_file, parse_env_text
from line_assistant.transport import LiveDisabled, RestClient


ROOT = Path(__file__).parents[1]
SECRET = "synthetic-secret-DO-NOT-LOG"
TOKEN = "synthetic-token-DO-NOT-LOG"
MODEL_KEY = "synthetic-model-key-DO-NOT-LOG"


def live_values():
    # No real account IDs/credentials, no dependency on ignored local-data or .env.
    return {"APP_MODE": "live", "LINE_OWNER_ID": "U" + "1" * 32,
            "LINE_CHANNEL_SECRET": SECRET, "LINE_CHANNEL_ACCESS_TOKEN": TOKEN,
            "GCP_PROJECT_ID": "synthetic-test-project", "SHEETS_SPREADSHEET_ID": "synthetic-sheet-id-000000001",
            "SHEETS_EXPENSE_SHEET_ID": "0", "SHEETS_DATABASE_SHEET_ID": "1"}


def test_example_declares_every_supported_key_and_is_safe_without_private_files():
    values = parse_env_text((ROOT / ".env.example").read_text())
    assert set(values) == ENV_KEYS
    config = load_config(values)
    assert config.mode == "mock" and config.environment == "local"
    assert config.timezone == "Asia/Bangkok" and config.firestore_database_id == "(default)"
    assert config.channel_secret is config.channel_access_token is config.model_api_key is None
    assert config.model_id is config.model_provider is None and not config.live_requests_enabled


def test_only_caller_supplied_configuration_is_read(monkeypatch):
    # No read of the user's existing .env or actual credential environment.
    monkeypatch.setenv("APP_MODE", "live")
    monkeypatch.setenv("LINE_CHANNEL_SECRET", SECRET)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", "forbidden-credential-path")
    def forbidden(*args, **kwargs):
        raise AssertionError("Implicit file access")
    monkeypatch.setattr(Path, "open", forbidden)
    config = load_config({})
    assert config.mode == "mock" and config.project_id is None and config.channel_secret is None


def test_live_profile_can_be_staged_but_never_starts_or_loads_a_token():
    config = load_config(live_values())
    assert config.sheet_expense_sheet_id == 0 and config.sheet_database_sheet_id == 1
    assert not config.live_requests_enabled and config.calendar_id is config.model_id is None
    with pytest.raises(LiveDisabled):
        create_app_from_config(config)
    called = []
    for service in ("sheets", "calendar", "firestore", "line"):
        client = RestClient(service, token=lambda: called.append(True))
        try:
            with pytest.raises(LiveDisabled):
                client.request("GET", "/no-network")
        finally:
            client.close()
    assert called == []


def test_mock_factory_uses_public_settings_even_if_private_values_are_staged():
    values = live_values()
    values["APP_MODE"] = "mock"
    config = load_config(values)
    app = create_app_from_config(config)
    assert app.state.assistant.settings.owner_id == "mock-owner"
    assert app.state.assistant.settings.channel_secret == "mock-channel-secret"
    client = TestClient(app)
    assert client.get("/health").json() == {"status": "ok", "mode": "mock"}
    assert SECRET not in repr(app.state.assistant.settings)


def test_cloud_run_staging_requires_identity_and_region_and_still_blocks_startup():
    values = live_values()
    values["APP_ENVIRONMENT"] = "cloud_run"
    with pytest.raises(ConfigError):
        load_config(values)
    values["GCP_REGION"] = "asia-southeast1"
    with pytest.raises(ConfigError):
        load_config(values)
    values["GCP_RUNTIME_SERVICE_ACCOUNT_EMAIL"] = "line-assistant@synthetic-test-project.iam.gserviceaccount.com"
    config = load_config(values)
    with pytest.raises(LiveDisabled):
        create_app_from_config(config)
    with pytest.raises(LiveDisabled):
        create_app_from_config(load_config({"APP_ENVIRONMENT": "cloud_run"}))


@pytest.mark.parametrize("key", ["LINE_OWNER_ID", "LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "GCP_PROJECT_ID",
                                 "SHEETS_SPREADSHEET_ID", "SHEETS_EXPENSE_SHEET_ID", "SHEETS_DATABASE_SHEET_ID"])
def test_live_staging_refuses_missing_required_values_without_echoing_secrets(key):
    values = live_values()
    del values[key]
    with pytest.raises(ConfigError) as caught:
        load_config(values)
    assert key in str(caught.value)
    assert all(secret not in str(caught.value) for secret in (SECRET, TOKEN, MODEL_KEY))


@pytest.mark.parametrize("key,value", [
    ("APP_MODE", "production"), ("APP_ENVIRONMENT", "remote"), ("APP_TIMEZONE", "UTC"),
    ("LIVE_REQUESTS_ENABLED", "true"), ("LIVE_REQUESTS_ENABLED", "1"),
    ("GCP_PROJECT_ID", "bad/../project"), ("GCP_REGION", "https://wrong.example"),
    ("GCP_RUNTIME_SERVICE_ACCOUNT_EMAIL", "person@example.com"), ("FIRESTORE_DATABASE_ID", "bad/database"),
    ("FIRESTORE_DATABASE_ID", ""), ("SHEETS_SPREADSHEET_ID", "https://wrong.example"),
    ("SHEETS_EXPENSE_SHEET_ID", "-1"), ("SHEETS_DATABASE_SHEET_ID", "1.2"),
    ("SHEETS_MAX_ROWS", "10001"), ("SHEETS_MAX_ROWS", "1"), ("SHEETS_MAX_ROWS", ""),
    ("CALENDAR_ID", "https://wrong.example"), ("LINE_OWNER_ID", "group owner"),
    ("SHEETS_EXPENSE_TAB", "bad\nname"), ("MODEL_PROVIDER", "has space"),
    ("LINE_CHANNEL_SECRET", 42), ("SHEETS_EXPENSE_SHEET_ID", True), ("LINE_OWNER_ID", [SECRET]),
])
def test_invalid_configuration_refuses_values_and_unsafe_resource_segments(key, value):
    values = live_values()
    values[key] = value
    with pytest.raises(ConfigError) as caught:
        load_config(values)
    assert all(secret not in str(caught.value) for secret in (SECRET, TOKEN, MODEL_KEY))


@pytest.mark.parametrize("change", ["equal_ids", "equal_tabs", "mock_owner", "mock_secret", "mock_token", "unknown_prefix"])
def test_conflicting_or_public_live_bindings_are_refused(change):
    values = live_values()
    if change == "equal_ids":
        values["SHEETS_DATABASE_SHEET_ID"] = "0"
    elif change == "equal_tabs":
        values["SHEETS_DATABASE_TAB"] = "All Expenses"
    elif change == "mock_owner":
        values["LINE_OWNER_ID"] = "mock-owner"
    elif change == "mock_secret":
        values["LINE_CHANNEL_SECRET"] = "mock-channel-secret"
    elif change == "mock_token":
        values["LINE_CHANNEL_ACCESS_TOKEN"] = "placeholder-not-a-credential"
    else:
        values["LINE_SECRET" + SECRET] = TOKEN
    with pytest.raises(ConfigError):
        load_config(values)


def test_config_repr_summary_and_wrapped_secrets_never_log_private_values(caplog, capsys):
    values = live_values()
    values.update(MODEL_PROVIDER="verified-later", MODEL_ID="unknown-model-id", MODEL_API_KEY=MODEL_KEY)
    config = load_config(values)
    assert config.channel_secret.get_secret_value() == SECRET
    output = repr(config) + str(config) + json.dumps(config.safe_summary()) + repr(asdict(config))
    output += repr(Settings(channel_secret=SECRET))
    assert all(secret not in output for secret in (SECRET, TOKEN, MODEL_KEY))
    assert caplog.text == "" and capsys.readouterr().out == ""


@pytest.mark.parametrize("key", ["LINE_CHANNEL_SECRET", "LINE_CHANNEL_ACCESS_TOKEN", "MODEL_API_KEY"])
def test_invalid_secret_errors_and_tracebacks_do_not_echo_inputs(key):
    values = live_values()
    values[key] = SECRET + "\n"
    try:
        load_config(values)
    except ConfigError as error:
        assert key in str(error)
        assert SECRET not in "".join(traceback.format_exception(error))
    else:
        pytest.fail("Invalid secret was accepted")


@pytest.mark.parametrize("values", [{"MODEL_PROVIDER": "unverified"}, {"MODEL_ID": "unverified"}, {"MODEL_API_KEY": MODEL_KEY}])
def test_incomplete_model_configuration_is_not_guessed(values):
    with pytest.raises(ConfigError):
        load_config(values)


def test_strict_explicit_file_loading_preserves_literal_values_and_never_executes(tmp_path):
    path = tmp_path / "synthetic-config.txt"
    literal = "$(touch${IFS}NEVER)${HOME}#literal"
    path.write_text('APP_MODE="mock"\nSHEETS_EXPENSE_TAB=All Expenses # comment\nLINE_CHANNEL_SECRET=' + repr(literal) + '\n')
    config = load_config_file(path)
    # Arbitrary shell content is literal configuration, never eval/source/expanded.
    assert config.channel_secret.get_secret_value() == literal


@pytest.mark.parametrize("text", ["export APP_MODE=mock", "BAD_SECRET_NAME=" + SECRET,
                                  "APP_MODE=mock\nAPP_MODE=live", "APP_MODE='unfinished", "not-a-key-" + SECRET, "\ud800"])
def test_strict_file_parse_errors_are_redacted(text):
    with pytest.raises(ConfigError) as caught:
        parse_env_text(text)
    assert SECRET not in str(caught.value)


def test_explicit_file_io_errors_do_not_expose_path_or_raw_bytes(tmp_path):
    with pytest.raises(ConfigError) as caught:
        load_config_file(tmp_path / SECRET)
    assert SECRET not in str(caught.value)
    path = tmp_path / "synthetic-config.txt"
    path.write_bytes(SECRET.encode() + b"\xff")
    with pytest.raises(ConfigError) as caught:
        load_config_file(path)
    assert SECRET not in str(caught.value)


def test_future_auth_ports_are_disabled_by_default_and_compatible_with_rest_injection():
    providers = TokenProviders()
    for provider in (providers.google_cloud, providers.google_workspace, providers.line, providers.model):
        with pytest.raises(LiveDisabled):
            provider()
    services = MockApiServices()
    with pytest.raises(LiveDisabled):
        RestClient("sheets", services.transport(), token=providers.google_workspace).request("GET", "/blocked-auth")
    assert services.calls == []
    assert "<redacted>" in repr(TokenProviders(line=lambda: TOKEN))
