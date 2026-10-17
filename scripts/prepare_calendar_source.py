"""Offline exact-input Calendar source packager; no image or network actions."""
from pathlib import Path
import argparse,json
import prepare_build_source as base

EXTRA=tuple('src/line_assistant/'+name+'.py' for name in (
    'conversation_mocks','domain_notes','domain_tools','domain_runtime','tool_loop','draft_patch',
    'history_context','history_dates','history_first','semantic_tools','raw_history',
    'history_persistence','history_integration','production_history_state',
    'production_history','history_cloud_run','expense_evidence','draft_workflow','catalog_names',
    'expense_sources','validation_explanations','calendar_dates','calendar_provider','calendar_workflow',
    'calendar_transport','calendar_sources','calendar_batches','response_realization','calendar_event_types','read_queries',
    'saved_edits','expense_edits','calendar_reschedule','saved_edit_transport','provisional_categories'))+('scripts/verify_history_signed_startup.py','deploy/history-production.Dockerfile')
INPUTS=base.BUILD_INPUTS+EXTRA

def prepare(root,output):
    old=base.BUILD_INPUTS
    try:base.BUILD_INPUTS=INPUTS;return base.prepare(root,output)
    finally:base.BUILD_INPUTS=old

def verify(output):
    old=base.BUILD_INPUTS
    try:base.BUILD_INPUTS=INPUTS;return base.verify(output)
    finally:base.BUILD_INPUTS=old

if __name__=='__main__':
    parser=argparse.ArgumentParser()
    group=parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--output',type=Path);group.add_argument('--verify',type=Path)
    args=parser.parse_args()
    print(json.dumps(verify(args.verify) if args.verify else prepare(Path(__file__).resolve().parents[1],args.output),indent=2))
