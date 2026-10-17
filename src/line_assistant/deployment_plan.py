"""Non-executable deployment checklist. Values and budgets belong in local config."""
from .model_api import MODEL_ID
from .pilot_config import SECRET_PATHS

def launch_plan():
    return {
        "execution_enabled": False,
        "launch_authorized": False,
        "model_id": MODEL_ID,
        "budget_configuration_required": True,
        "monthly_micro_usd": 0,
        "secrets": [{"mount": value, "resource_name_configured_by_operator": True} for value in SECRET_PATHS.values()],
        "checks": ["explicit target/schema and attached service account", "signed owner-only ingress", "one immutable image for candidate and compatible OFF bridge", "health/signature and zero-provider startup", "preserve raw history, receipts and unknown effects"],
    }
