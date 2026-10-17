import importlib.util
import json
from pathlib import Path

import pytest

from line_assistant.runtime_config import ConfigError
from test_cloud_run import fake_secrets
from test_workbook_switch import production_values


spec = importlib.util.spec_from_file_location("verify_signed_startup", Path(__file__).parents[1] / "scripts/verify_signed_startup.py")
startup = importlib.util.module_from_spec(spec)
spec.loader.exec_module(startup)


def test_signed_empty_and_nonowner_check_returns_only_fixed_metadata_and_no_providers(caplog, capsys):
    loaded = []
    def loader(name):
        loaded.append(name)
        return fake_secrets(name)
    result = startup.run_checks(production_values(), secret_loader=loader)
    assert result == {"status": "passed", "signed_empty_http_status": 200, "invalid_signature_http_status": 401,
                      "nonowner_ignored": True, "provider_requests": 0}
    assert loaded == ["line-channel-secret", "line-channel-access-token", "openai-api-key"]
    output = json.dumps(result) + caplog.text + capsys.readouterr().out
    assert "synthetic-line-channel-secret" not in output and "synthetic-openai-api-key" not in output


@pytest.mark.parametrize("key,value", [("PILOT_LAUNCH_AUTHORIZED", "false"),
    ("PILOT_MODEL_MONTHLY_MICRO_USD", "499999"), ("PILOT_MODEL_MAX_CALLS_DAY", "19"),
    ("PILOT_MODEL_MAX_CALLS_MONTH", "299")])
def test_startup_check_requires_exact_authorized_configuration_before_secret_load(key, value):
    values = {**production_values(), key: value}
    with pytest.raises(ConfigError):
        startup.run_checks(values, secret_loader=lambda _: pytest.fail("Loaded secret"))


def test_temporary_entrypoint_failure_never_prints_exception_credentials(monkeypatch, capsys):
    def failure(_):
        raise ValueError("PRIVATE_TOKEN_DIAGNOSTIC")
    monkeypatch.setattr(startup, "run_checks", failure)
    with pytest.raises(SystemExit) as error:
        startup.main()
    assert error.value.code == 1
    output = capsys.readouterr().out
    assert "PRIVATE_TOKEN_DIAGNOSTIC" not in output
    assert output.strip() == 'production_signed_check_result {"status":"failed"}'
