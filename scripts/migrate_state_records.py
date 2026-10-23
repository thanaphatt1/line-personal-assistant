"""One-owner state migration inside the reviewed runtime, without business I/O.

Use the same image, mounted secrets, service account and explicit configuration
as the service. Preview is read-only. Apply resumes the durable migration cursor;
it never handles or replays a LINE message. No credentials or policies are set up.
"""
import argparse
import json
import os

from line_assistant.firestore_state import LeaseBusy
from line_assistant.history_cloud_run import create_history_cloud_run_app,HISTORY_KEYS
from line_assistant.history_context import digest
from line_assistant.pilot_config import PILOT_KEYS
from line_assistant.pilot_http import rpc_budget
from line_assistant.snapshot_codec import decode
from line_assistant.state_record_migration import records
from line_assistant.transport import LiveDisabled,NotReady,ApiError,OutcomeUnknown
from line_assistant.firestore_state import LeaseLost


def run(app,*,apply=False,verify=False):
    http=app.state.pilot_http;state=app.state.assistant.state
    original=http.allowed
    def state_only(service,method,path,body,params):
        if service!='firestore':raise LiveDisabled('Migration cannot call business, model or LINE services')
        store=state.record_store
        if store.authorize(method,path,body,params):return original(service,method,path,body,params)
        if path=='/v1/'+state.name and method=='GET':return original(service,method,path,body,params)
        if path=='/v1/'+state.database+'/documents:commit':
            writes=body.get('writes',[]) if isinstance(body,dict) else []
            if len(writes)==1 and writes[0].get('update',{}).get('name')==state.name:
                return original(service,method,path,body,params)
        from line_assistant.raw_history import FirestoreJournal
        parents={FirestoreJournal(None,state.database,state.owner_id,book).parent for book in state.approved_workbooks}
        if method=='GET' and any(path.startswith('/v1/'+p+'/assistant_raw_messages/') for p in parents):
            return original(service,method,path,body,params)
        if method=='POST' and path in {'/v1/'+p+':runQuery' for p in parents}:
            return original(service,method,path,body,params)
        raise LiveDisabled('Migration is limited to owned workflow evidence')
    http.allowed=state_only
    try:
        with rpc_budget(512,seconds=100):
            if not apply:
                doc=state._read()
                if doc is None:raise NotReady('Existing owner state is required')
                value=decode(doc['fields']['snapshot']['stringValue'],state.max_snapshot_bytes)
                marker=value.get('state_records_v1',{})
                if verify:
                    if value.get('version')!=2 or marker.get('phase')!='ready':raise NotReady('Migration has not completed')
                    state._restore_state(value)
                    after=state._read()
                    if after is None or after['updateTime']!=doc['updateTime']:raise NotReady('Owner state changed during verification')
                    return {'status':'verified','version':2,'phase':'ready','business_requests':0}
                return {'status':'preview','version':value['version'],'phase':marker.get('phase','inline'),
                    'snapshot_sha256':digest(value),
                    'record_count':len(records(value,state.approved_workbooks)) if value['version']==1 else None,
                    'business_requests':0}
            if not http.config.state_records_enabled:raise NotReady('Apply requires explicit state-records activation')
            progress=[0]
            previous_hook=state.record_phase_hook
            def phase(name,cursor):
                progress[0]=max(progress[0],cursor)
                previous_hook(name,cursor)
            state.record_phase_hook=phase
            try:
                with state.session():
                    if state.record_store.mode!='ready':raise NotReady('Evidence migration is incomplete')
                    version=state._snapshot_state()['version']
            finally:state.record_phase_hook=previous_hook
            return {'status':'ready','version':version,'copied_records':progress[0],'business_requests':0}
    finally:http.allowed=original


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    mode=parser.add_mutually_exclusive_group()
    mode.add_argument('--apply',action='store_true',help='Apply or resume the owner-evidence migration')
    mode.add_argument('--verify',action='store_true',help='Read and validate normalized state without acquiring a lease or writing')
    args=parser.parse_args()
    app=None
    try:
        app=create_history_cloud_run_app({k:os.environ[k] for k in PILOT_KEYS|HISTORY_KEYS if k in os.environ})
        result=run(app,apply=args.apply,verify=args.verify)
    except LeaseBusy:
        print(json.dumps({'status':'failed','reason':'owner_busy'}),flush=True)
        raise SystemExit(2) from None
    except Exception as error:
        # Provider prose, model/LINE tokens and owner evidence never reach logs.
        reason=('lease_lost' if isinstance(error,LeaseLost) else 'provider_error' if isinstance(error,ApiError)
                else 'outcome_unknown' if isinstance(error,OutcomeUnknown) else 'not_ready' if isinstance(error,NotReady)
                else 'scope_blocked' if isinstance(error,LiveDisabled) else 'invalid_state' if isinstance(error,ValueError)
                else 'migration_unverified')
        print(json.dumps({'status':'failed','reason':reason,
            **({'http_status':error.status} if isinstance(error,ApiError) else {})}),flush=True)
        raise SystemExit(1) from None
    finally:
        if app is not None:app.state.pilot_http.close()
    print(json.dumps(result,separators=(',',':')),flush=True)


if __name__=='__main__':main()
