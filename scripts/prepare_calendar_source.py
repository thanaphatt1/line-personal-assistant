"""Offline exact-input Calendar source packager; no image or network actions."""
from pathlib import Path
import argparse,json
import prepare_build_source as base

EXTRA=tuple('src/line_assistant/'+name+'.py' for name in (
    'firestore_expenses','expense_initialization',
    'conversation_mocks','domain_notes','domain_tools','domain_runtime','tool_loop','draft_patch',
    'history_context','history_dates','history_first','semantic_tools','raw_history','planner_instructions','catalog_aliases','record_actions',
    'performance','read_batch','clarifications',
    'history_persistence','history_integration','production_history_state','state_records','state_record_codec','state_record_migration','state_record_cleanup','relational','relational_sql',
    'production_history','history_cloud_run','expense_evidence','draft_workflow','catalog_names',
    'expense_sources','validation_explanations','pending_records','pending_catalog','tool_errors','tool_repair','temporal','calendar_dates','calendar_provider','calendar_workflow',
    'calendar_transport','calendar_sources','calendar_batches','calendar_connections','calendar_inference','query_analysis','response_realization','answer_composition','operation_composition','name_matching','calendar_event_types','read_queries',
    'saved_edits','expense_edits','calendar_reschedule','saved_edit_transport','provisional_categories','historical_categories','previous_prices','data_inventory','query_followups','tables','table_storage','table_workflow'))+('scripts/verify_history_signed_startup.py','scripts/migrate_state_records.py','deploy/history-production.Dockerfile')
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
