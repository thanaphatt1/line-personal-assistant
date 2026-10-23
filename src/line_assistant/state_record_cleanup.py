"""Bounded removal of expired proofs; unresolved effects are always retained."""
from copy import deepcopy
from datetime import datetime, timedelta

from . import state_record_codec as codec
from .firestore_state import LeaseLost
from .transport import ApiError, LiveDisabled, OutcomeUnknown


def prune(store):
    state=store.state
    previous=deepcopy(store.marker)
    cleanup=deepcopy(previous.get('cleanup',{}))
    at=cleanup.get('next_at')
    if at and datetime.fromisoformat(at)>state.clock():return
    if store.planned():return
    writes=[];removed=[]
    protected=set(store.protected_actions)
    # Restore validates batch/action bindings before ordinary TTL expiry runs.
    # Keep parent evidence while any retained workspace still references it.
    protected.update(b['action_key'] for b in state.calendar_batches.values() if b.get('action_key'))
    for parts in state.history_workspaces.values():
        protected.update(b['action_key'] for b in parts.get('calendar_batches_v1',{}).get('batches',{}).values() if b.get('action_key'))
    for (group,_),entry in list(store.entries.items()):
        if group!='actions' or not entry:continue
        a=entry['objects'].get('actions')
        raw=codec.dump('actions',a) if a else entry['row']['values']['actions']
        if raw['status']!='completed' and raw['metadata'].get('calendar_batch'):
            protected.update(m['key'] for m in raw['payload']['members'])
    try:
        for group in ('events','actions','models'):
            query={'from':[{'collectionId':'records'}],
                   'where':{'fieldFilter':{'field':{'fieldPath':'retain_until'},'op':'LESS_THAN_OR_EQUAL',
                            'value':{'timestampValue':state.clock().isoformat()}}},
                   'orderBy':[{'field':{'fieldPath':'retain_until'},'direction':'ASCENDING'},
                              {'field':{'fieldPath':'__name__'},'direction':'ASCENDING'}],
                   'limit':4}
            if cleanup.get(group):query['startAt']={'values':cleanup[group],'before':False}
            rows=store._request('POST','/v1/'+store.parent(group)+':runQuery',{'structuredQuery':query})
            docs=[r['document'] for r in rows if 'document' in r]
            if len(docs)>4:raise OutcomeUnknown('Expired evidence page exceeds bound')
            for doc in docs:
                entry=store._accept(doc,group);row=entry['row']
                active,until=codec.metadata(row)
                if active or until>state.clock():raise OutcomeUnknown('Expired evidence query returned a live record')
                if group=='actions' and row['key'] in protected:continue
                if group=='events':
                    claim=row['values'].get('claims') or {}
                    if claim.get('action_key'):
                        action=store._read('actions',claim['action_key'])
                        a=action['objects'].get('actions') if action else None
                        raw=codec.dump('actions',a) if a else action['row']['values'].get('actions') if action else None
                        if raw and (raw['status']!='completed' or datetime.fromisoformat(raw['expires_at'])>state.clock() or claim['action_key'] in protected):continue
                writes.append({'delete':doc['name'],'currentDocument':{'updateTime':doc['updateTime']}})
                removed.append((group,row['key']))
            cleanup[group]=([docs[-1]['fields']['retain_until'],{'referenceValue':docs[-1]['name']}]
                            if len(docs)==4 else None)
        cleanup['next_at']=(state.clock()+timedelta(minutes=15)).isoformat()
        store.marker={**previous,'cleanup':cleanup}
        from .state_record_migration import advance
        advance(store,state._record_snapshot(),writes)
        for key in removed:store.entries[key]=None
    except (ApiError,LiveDisabled,OutcomeUnknown,LeaseLost,KeyError,TypeError,ValueError):
        store.marker=previous
        from .action_trace import emit
        emit('record_cleanup',status='deferred')
