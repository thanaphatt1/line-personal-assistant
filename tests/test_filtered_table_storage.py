"""Indexed reads preserve local query semantics and transaction pagination."""
from copy import deepcopy
import json
import httpx
import pytest
from line_assistant.table_storage import FirestoreTableStore,Binding,encode
from line_assistant.tables import Table,Column,Predicate,Query,Registry,query,MemoryTableStore
from line_assistant.transport import RestClient,NotReady,OutcomeUnknown
from line_assistant.read_cache import scope
from test_table_storage import FirestoreServer

TABLE=Table(id='records',label='ข้อมูลจำลอง',description='Synthetic date and exact decimal query records',columns=[
    Column(name='day',label='วันที่',type='date',required=True),
    Column(name='category',label='ประเภท',type='text',required=True),
    Column(name='amount',label='ยอด',type='decimal',required=True),
    Column(name='number',label='ลำดับ',type='integer',required=True),
    Column(name='optional',label='รายละเอียด',type='text')])


class QueryServer(FirestoreServer):
    def __init__(self):super().__init__();self.queries=[];self.fail_page=None;self.replay_page=False;self.empty_schema=False
    def handle(self,request):
        if not request.url.path.endswith(':runQuery'):return super().handle(request)
        body=json.loads(request.content);self.queries.append(deepcopy(body))
        if self.fail_page==len(self.queries):raise httpx.ReadTimeout('synthetic unknown response',request=request)
        query=body['structuredQuery'];f=query['where']['fieldFilter'];name=f['field']['fieldPath']
        def value(v):return next(iter(v.values()))
        op=f['op'];wanted=value(f['value'])
        rows=[d for d in self.snapshots[body['transaction']].values() if name in d['fields']]
        def matches(d):
            actual=value(d['fields'][name])
            if 'integerValue' in f['value']:actual=int(actual);other=int(wanted)
            else:other=wanted
            return {'EQUAL':lambda:actual==other,'GREATER_THAN':lambda:actual>other,
                'GREATER_THAN_OR_EQUAL':lambda:actual>=other,'LESS_THAN':lambda:actual<other,
                'LESS_THAN_OR_EQUAL':lambda:actual<=other}[op]()
        rows=[d for d in rows if matches(d)]
        def key(d):
            return (int(value(d['fields'][name])) if 'integerValue' in f['value'] else value(d['fields'][name]),d['name']) if op!='EQUAL' else (d['name'],)
        rows.sort(key=key)
        cursor=query.get('startAt',{}).get('values')
        if cursor and not self.replay_page:
            last=cursor[-1]['referenceValue']
            index=next(i for i,d in enumerate(rows) if d['name']==last);rows=rows[index+1:]
        rows=rows[:query['limit']]
        return httpx.Response(200,json=[{'document':deepcopy(d)} for d in rows])


def setup(count=120,*,max_records=1000):
    server=QueryServer();store=FirestoreTableStore(RestClient('firestore',httpx.MockTransport(server.handle)),
        Binding(backend='firestore',collection_path='records',max_records=max_records),owner_id='synthetic',project_id='synthetic-project')
    records=[]
    for index in range(count):
        identifier=f'row{index:04d}';values={'day':'2026-10-21' if index%2 else '2026-10-22',
            'category':'A' if index%2 else 'B','amount':str(index)+'.10','number':index,'optional':None}
        name=store.collection+'/'+identifier
        server.documents[name]={'name':name,'updateTime':'2026-10-22T00:00:00Z',
                                'fields':{k:encode(v) for k,v in values.items() if v is not None}}
        records.append({'id':identifier,'version':'v1','values':values})
    return Registry([TABLE],{'records':store}),server,records


@pytest.mark.parametrize('filters',[
    [Predicate(field='category',op='eq',values=['A'])],
    [Predicate(field='day',op='gte',values=['2026-10-22'])],
    [Predicate(field='number',op='lt',values=[101])],
    [Predicate(field='category',op='eq',values=['A']),Predicate(field='amount',op='gt',values=['90.10'])],
])
def test_pushed_filter_and_local_residual_equal_complete_memory_query(filters):
    registry,server,records=setup(220)
    memory=Registry([TABLE],{'records':MemoryTableStore(records=records)})
    p=Query(table_id='records',filters=filters,order_by=[{'field':'number','descending':True}],limit=1,source_refs=['synthetic'])
    expected=query(memory,p);actual=query(registry,p)
    for key in ['count','result_count','rows','shown','truncated']:assert actual[key]==expected[key]
    assert len(server.queries)>=1 and server.queries[0]['structuredQuery']['limit']==100
    assert len({v['transaction'] for v in server.queries})==1
    assert server.calls[-1][1].endswith(':rollback')
    assert actual['count']>actual['shown']  # display limit never truncates the scan


def test_larger_collection_can_answer_small_matching_scope_without_full_scan():
    registry,server,_=setup(120,max_records=2)
    p=Query(table_id='records',filters=[Predicate(field='number',op='eq',values=[117])],source_refs=['synthetic'])
    result=query(registry,p)
    assert result['complete'] and result['count']==1 and result['rows'][0]['number']==117
    assert len(server.queries)==1 and not server.commits


@pytest.mark.parametrize('kind',['unknown','repeat','overflow'])
def test_partial_unknown_repeated_or_overflowed_page_is_never_a_complete_total(kind):
    registry,server,_=setup(220,max_records=5 if kind=='overflow' else 1000)
    server.fail_page=2 if kind=='unknown' else None;server.replay_page=kind=='repeat'
    p=Query(table_id='records',filters=[Predicate(field='category',op='eq',values=['A'])],source_refs=['synthetic'])
    with pytest.raises((NotReady,OutcomeUnknown)):query(registry,p)
    assert server.calls[-1][1].endswith(':rollback') and not server.commits


@pytest.mark.parametrize('f',[Predicate(field='amount',op='gt',values=['2.1']),
    Predicate(field='optional',op='eq',values=[None]),Predicate(field='category',op='contains',values=['a'])])
def test_decimal_null_and_contains_keep_exact_complete_scan_semantics(f):
    registry,server,records=setup(5)
    p=Query(table_id='records',filters=[f],source_refs=['synthetic'])
    actual=query(registry,p)
    expected=query(Registry([TABLE],{'records':MemoryTableStore(records=records)}),p)
    assert actual['count']==expected['count'] and not server.queries


def test_next_request_reloads_source_and_mutations_never_use_the_read_cache():
    registry,server,_=setup(2)
    p=Query(table_id='records',filters=[Predicate(field='category',op='eq',values=['A'])],source_refs=['synthetic'])
    with scope():
        assert query(registry,p)['count']==1
        first=query(registry,p);first['rows'].clear()
        assert query(registry,p)['shown']==1 and len(server.queries)==1
    name=next(k for k in server.documents if k.endswith('row0001'));server.documents.pop(name)
    with scope():assert query(registry,p)['count']==0
    assert len(server.queries)==2
