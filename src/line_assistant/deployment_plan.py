"""Non-executable deployment checklist. Values and budgets belong in local config."""
from .model_api import MODEL_ID, PLANNER_TURN_SECONDS
from .pilot_config import SECRET_PATHS


def check_request_timeout(service):
    """Offline gate for the exact operator-selected Cloud Run manifest."""
    minimum=launch_plan()['request_timeout_seconds']
    seconds=service.get('spec',{}).get('template',{}).get('spec',{}).get('timeoutSeconds')
    if type(seconds) is not int or seconds<minimum:
        raise ValueError('Cloud Run request timeout must cover assistant, finalization, delivery and cold start')
    return {'status':'passed','request_timeout_seconds':seconds,'minimum_seconds':minimum}

def launch_plan():
    return {
        "execution_enabled": False,
        "launch_authorized": False,
        "model_id": MODEL_ID,
        "budget_configuration_required": True,
        "monthly_micro_usd": 0,
        "request_timeout_seconds": max(90, PLANNER_TURN_SECONDS + 10 + 20),
        "secrets": [{"mount": value, "resource_name_configured_by_operator": True} for value in SECRET_PATHS.values()],
        "checks": ["explicit target/schema and attached service account", "signed owner-only ingress", "one immutable image for candidate and compatible OFF bridge", "health/signature and zero-provider startup", "request timeout covers cold start, assistant finalization and reply delivery", "preserve raw history, receipts and unknown effects"],
    }
