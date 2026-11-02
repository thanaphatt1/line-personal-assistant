"""Development-only signed runtime adapter with synthetic HTTP providers.

Requires the public tests/dev dependencies. All model calls go to the supplied
callback, including any auxiliary calls. No legacy tool adaptation, oracle or
reference plans are supplied to that callback. No credentials are discovered.
"""
from copy import deepcopy
from dataclasses import asdict
from datetime import datetime,date
import json
from pathlib import Path
import sys
from time import monotonic

ROOT=Path(__file__).resolve().parents[1]
sys.path[:0]=[str(ROOT/'src'),str(ROOT/'tests')]
import httpx
from fastapi.testclient import TestClient
from test_calendar_read_all import AllCalendarsHosted, PERSONAL, WORK
from test_read_queries import event as calendar_event
from state_records_hosted import RecordsProtocol
from line_assistant.sheet_dates import date_to_serial
from line_assistant import answer_composition
from line_assistant.line_api import rendered_message
from semantic_oracle import fingerprint,score_turn


class SyntheticRuntime(RecordsProtocol,AllCalendarsHosted):
    def __init__(self,fixtures,model,*,provider_failure=False):
        self.model_callback=model;self.provider_failure=provider_failure;self.provider_reads=0;self.provider_faults=0;self.model_errors=[];self.model_outputs=[]
        super().__init__()
        self.now=datetime.fromisoformat(fixtures['now']);self.extra_config={'PILOT_STATE_RECORDS_ENABLED':'true'}
        self.google.cells={k:v for k,v in self.google.cells.items() if k[0] not in {'All Expenses','Database'} or k[1]==1}
        for index,row in enumerate(fixtures['expense'],2):
            for column,value in enumerate([date_to_serial(date.fromisoformat(row['date'])),row['item'],float(row['amount']),row.get('note',''),'synthetic-expense-'+str(index)],1):
                self.google.cells[('All Expenses',index,column)]=value
        for index,row in enumerate(fixtures['catalog'],2):
            for column,key in enumerate(['item','category','subcategory'],1):self.google.cells[('Database',index,column)]=row[key]
        self.calendar.events={}
        for index,row in enumerate(fixtures['calendar']):
            value={**calendar_event('seed-'+str(index),row['date']+'T09:00:00+07:00',row['date']+'T10:00:00+07:00'),'summary':row['title']}
            self.calendar.events[value['id']]=value
        self.event_pages={WORK:{'':{'items':[]}},PERSONAL:{'':{'items':[]}}}

    def handle(self,request):
        if request.url.host=='api.openai.com':
            body=json.loads(request.content);self.model_bodies.append(deepcopy(body))
            # No retry and no fallback response supplied by this harness.
            try:value=self.model_callback(deepcopy(body))
            except Exception as error:
                self.model_errors.append(type(error).__name__);raise
            if not isinstance(value,dict):
                self.model_errors.append('ValueError');raise ValueError('model callback must return a Responses envelope')
            self.model_outputs.append(deepcopy(value))
            return httpx.Response(200,json=value)
        if request.url.host in {'sheets.googleapis.com','www.googleapis.com'} and request.method=='GET':
            self.provider_reads+=1
            if getattr(self,'provider_failure',False) and '/values/' in request.url.path and 'All' in str(request.url):
                self.provider_faults+=1;return httpx.Response(403,json={})
        return super().handle(request)

    def business(self):
        def cells(tab):return [[list(k[1:]),v] for k,v in sorted(self.google.cells.items()) if k[0]==tab and k[1]>1]
        def calendar(r):return {'title':r['summary'],'start':r['start'].get('dateTime',r['start'].get('date')),'end':r['end'].get('dateTime',r['end'].get('date'))}
        return {'expense':cells('All Expenses'),'catalog':cells('Database'),'calendar':{k:calendar(v) for k,v in self.calendar.events.items()}}

    def writes(self):return self.google.sheet_writes+self.calendar.posts


def grounded_document(api,result,first_call):
    """Re-render the accepted document from the actual offered runtime facts."""
    realization=result.data.get('response_realization',{})
    wanted=realization.get('response_ref')
    if not wanted:return None
    for body,value in zip(api.model_bodies[first_call:],api.model_outputs[first_call:]):
        context=json.loads(body['input'][0]['content'])
        offered={v['response_ref']:v for v in context.get('read_responses',[])}
        for call in value.get('output',[]):
            if call.get('type')!='function_call' or call.get('name')!='act':continue
            try:
                request=json.loads(call['arguments'])['request']
                if request.get('intent')!='respond' or request.get('response_ref')!=wanted:continue
                facts=offered[wanted]['facts'];document=request['document']
                if realization.get('status')=='fallback':
                    if realization.get('reason')!='invalid_answer_document':continue
                    # A status flag alone cannot award grounding. Confirm the
                    # received document failed for the recorded reason, then
                    # independently reproduce the complete fallback and reply.
                    try:answer_composition.render(document,facts)
                    except answer_composition.AnswerError as error:
                        if str(error)!=realization.get('validation_code'):continue
                    else:continue
                    document=answer_composition.fallback_document(facts)
                text=answer_composition.render(document,facts)
                if text==result.message:return True
            except (ValueError,KeyError,TypeError):continue
    return False


class AttemptFailure(RuntimeError):
    """A failed attempt retains completed turns and unknown model usage."""
    def __init__(self,kind,partial):
        super().__init__('Synthetic evaluation attempt failed')
        self.kind=kind;self.partial=deepcopy(partial)


def received_usage(value):
    """Keep numeric accounting fields only, never arbitrary response content."""
    usage=value.get('usage') if isinstance(value,dict) else None
    if not isinstance(usage,dict):return None
    result={k:usage[k] for k in ['input_tokens','output_tokens','total_tokens'] if type(usage.get(k)) is int and usage[k]>=0}
    for key in ['input_tokens_details','output_tokens_details']:
        detail=usage.get(key)
        if isinstance(detail,dict):result[key]={k:detail[k] for k in ['cached_tokens','cache_write_tokens','reasoning_tokens'] if type(detail.get(k)) is int and detail[k]>=0}
    return result or None


def run_case(case,fixtures,model):
    """Fresh state per scenario; native model callback receives only runtime body.

    The HTTP transport is wholly mocked, so only a caller-supplied callback can
    contact a real model. Such a caller is responsible for explicit authorization,
    secure credential handling and network policy. The CLI never does that.
    """
    audit={'scenario_id':case['id'],'turns':[],'request_hashes':[],'received_usage':[],
           'model_profile':[],'callback_failures':[]}
    def tracked(body):
        audit['request_hashes'].append(fingerprint(body));audit['received_usage'].append(None)
        audit['model_profile'].append({k:body.get(k) for k in ['model','reasoning','max_output_tokens','temperature']})
        try:value=model(body)
        except Exception as error:
            audit['callback_failures'].append(type(error).__name__);raise
        audit['received_usage'][-1]=received_usage(value)
        return value
    try:
        _run_case(case,fixtures,tracked,audit['turns'])
        return {**audit,'status':'completed'}
    except Exception as error:
        raise AttemptFailure(type(error).__name__,audit) from None


def _run_case(case,fixtures,model,turns):
    seeded=deepcopy(fixtures)
    seeded['catalog']=[r for r in seeded['catalog'] if r['item'] not in case.get('catalog_omit',[])]
    api=SyntheticRuntime(seeded,model,provider_failure=case.get('provider_failure',False))
    app=api.app();host=app.state.assistant;captured=[];original=host.handle;traces=[]
    host.action_trace_sink=traces.append
    def capture(e):
        value=original(e);captured.append(deepcopy(value));return value
    host.handle=capture
    events=[];last=None
    with TestClient(app) as client:
        for index,turn in enumerate(case['turns']):
            text=turn['text']
            if text=='$confirm':text=last.data['confirm_command']
            postback=False
            if text=='$cancel':
                buttons=last.data['line_message']['contents']['footer']['contents']
                text=next(b['action']['data'] for b in buttons if '|cancel|' in b['action'].get('data',''));postback=True
            e=deepcopy(events[-1]) if text=='$replay' else api.event(text,postback=postback)
            before=api.business();writes=api.writes();calls=len(api.model_bodies);reads=api.provider_reads;faults=api.provider_faults;deliveries=len(api.google.line_replies)
            started=monotonic();response=api.send(client,e);elapsed=round((monotonic()-started)*1000,3)
            if api.model_errors:raise RuntimeError('Model callback failed; run aborted without substituting a response')
            if response.status_code!=200 or not captured:raise RuntimeError('synthetic webhook failed; inspect private local run')
            last=captured[-1];events.append(e)
            output=last.data.get('tool_result',{});query_reads=getattr(host,'_query_reads',{})
            reported=[deepcopy(query_reads[ref]) for ref in output.get('query_refs',[]) if ref in query_reads]
            observation={'result':asdict(last),'reported_results':reported,'before':before,'after':api.business(),
                'write_delta':api.writes()-writes,'model_calls':len(api.model_bodies)-calls,'provider_reads':api.provider_reads-reads,
                'provider_faults':api.provider_faults-faults,
                'delivered':len(api.google.line_replies)==deliveries+1 and api.google.line_replies[-1]==[rendered_message(last)],
                'grounded_document':grounded_document(api,last,calls),'elapsed_ms':elapsed,
                'realization_mode':last.data.get('response_realization',{}).get('mode',last.data.get('response_realization',{}).get('status','none'))}
            score=score_turn(fixtures,turn['gold'],observation)
            turns.append({'turn':index+1,'scores':score,'observation':observation})
            traces.clear()
