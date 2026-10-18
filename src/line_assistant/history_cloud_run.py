"""Separate default-off history factory; frozen legacy Cloud Run files intact."""
from contextlib import asynccontextmanager
from dataclasses import dataclass
import hashlib,json,re

from .cloud_run import create_cloud_run_app as legacy_app,DisabledCalendar,BoundedReplies
from .pilot_config import PilotConfig,PILOT_KEYS
from .pilot_http import PilotHttp,PilotPort
from .main import create_app
from .catalog import ItemCatalog
from .production_history_state import ProductionHistoryState
from .production_history import ProductionHistoryAssistant
from .conversation import ConversationAssistant
from .intent_api import IntentPlanner
from .model_api import DurableModelGuard,MAX_BODY_BYTES,MAX_OUTPUT_TOKENS,MODEL_ID,RESERVATION_MICRO_USD
from .history_first import tools,INSTRUCTIONS,ENROLLMENT_INSTRUCTIONS,SEMANTIC_NOTE,SEMANTIC_INSTRUCTIONS
from .sheets_api import SheetsApi
from .line_api import LineReplyApi
from .transport import LiveDisabled
from .runtime_config import ConfigError
from . import raw_history as raw


HISTORY_KEYS=frozenset({'PILOT_HISTORY_ENABLED','PILOT_HISTORY_STATE_ENABLED','PILOT_CALENDAR_ENABLED','PILOT_CALENDAR_ID','PILOT_RESPONSE_REALIZATION_ENABLED','PILOT_MODEL_USAGE_GATES_ENABLED','PILOT_SAVED_EXPENSE_EDITS_ENABLED','PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED','PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY','PILOT_PROVISIONAL_CATEGORIES_ENABLED','PILOT_PROVISIONAL_MAPPING_ENABLED','PILOT_HISTORICAL_CATEGORIES_ENABLED'})

@dataclass(frozen=True,repr=False)
class HistoryConfig:
    legacy: PilotConfig
    history_enabled: bool
    history_state_enabled: bool
    calendar_enabled: bool=False
    calendar_id: str|None=None
    response_realization_enabled: bool=False
    usage_gates_enabled: bool=True
    saved_expense_enabled: bool=False
    saved_calendar_enabled: bool=False
    saved_expense_concurrency: str='off'
    provisional_categories_enabled: bool=False
    provisional_mapping_enabled: bool=False
    historical_categories_enabled: bool=False
    def __getattr__(self,key):return getattr(self.legacy,key)
    @classmethod
    def from_mapping(cls,values):
        h=values.get('PILOT_HISTORY_ENABLED','false');s=values.get('PILOT_HISTORY_STATE_ENABLED','false')
        core=PilotConfig.from_mapping({k:v for k,v in values.items() if k not in HISTORY_KEYS})
        if (h not in {'true','false'} or s not in {'true','false'} or h=='true' and s!='true'
                or s=='true' and (not core.launch_authorized or core.mode!='production' or not core.conversation_enabled or core.domain_tool_loop_enabled)):
            raise ConfigError('Complete explicit history conversation/state configuration required')
        calendar=values.get('PILOT_CALENDAR_ENABLED','false');target=values.get('PILOT_CALENDAR_ID','')
        if (calendar not in {'true','false'} or not isinstance(target,str) or len(target)>512
            or target and not re.fullmatch(r'[A-Za-z0-9_.+-]+@group\.calendar\.google\.com',target)
            or calendar=='true' and (h!='true' or s!='true' or not target)):
            raise ConfigError('Explicit separate Calendar ID and history opt-in required')
        voice=values.get('PILOT_RESPONSE_REALIZATION_ENABLED','false')
        if voice not in {'true','false'} or voice=='true' and (h!='true' or s!='true'):
            raise ConfigError('Response realization requires explicit history/state opt-in')
        usage=values.get('PILOT_MODEL_USAGE_GATES_ENABLED','true')
        if usage not in {'true','false'} or usage=='false' and s!='true':
            raise ConfigError('Observational usage requires explicit single-owner history state')
        expense=values.get('PILOT_SAVED_EXPENSE_EDITS_ENABLED','false');reschedule=values.get('PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED','false')
        concurrency=values.get('PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY','off')
        if (expense not in {'true','false'} or reschedule not in {'true','false'} or concurrency not in {'off','reviewed_single_writer'}
            or (expense=='true' or reschedule=='true') and (h!='true' or s!='true')
            or expense=='true' and concurrency!='reviewed_single_writer' or reschedule=='true' and calendar!='true'):
            raise ConfigError('Saved-record edits require explicit flags, history and reviewed concurrency policy')
        labels=values.get('PILOT_PROVISIONAL_CATEGORIES_ENABLED','false');mappings=values.get('PILOT_PROVISIONAL_MAPPING_ENABLED','false')
        if (labels not in {'true','false'} or mappings not in {'true','false'} or labels=='true' and (h!='true' or s!='true')
            or mappings=='true' and (labels!='true' or concurrency!='reviewed_single_writer')):raise ConfigError('Provisional mapping requires explicit flags, history and reviewed Database writer policy')
        historical=values.get('PILOT_HISTORICAL_CATEGORIES_ENABLED','false')
        if historical not in {'true','false'} or historical=='true' and labels!='true':raise ConfigError('Historical categories require explicit provisional/history configuration')
        return cls(core,h=='true',s=='true',calendar=='true',target or None,voice=='true',usage=='true',expense=='true',reschedule=='true',concurrency,labels=='true',mappings=='true',historical=='true')

class HistoryHttp(PilotHttp):
    def request(self,service,method,path,*,body=None,params=None,headers=None):
        if service=='calendar' and method=='PATCH':
            from .saved_edit_transport import mutation
            if not mutation(self,service,method,path,body,params,headers):raise LiveDisabled('Unconfirmed conditional reschedule')
            token=self.google_token('calendar_events')
            return self._json(self._http(method,'https://www.googleapis.com'+path,json=body,params=params,
                headers={'Authorization':'Bearer '+token,**headers}))
        return super().request(service,method,path,body=body,params=params,headers=headers)
    def _json(self,response):
        if getattr(self,'raw_history_enabled',False) and response.request.url.path.endswith(':runQuery'):
            value=response.json()
            if isinstance(value,list):return value
            raise LiveDisabled('Invalid paginated history response')
        return super()._json(response)
    def allowed(self,service,method,path,body,params):
        from . import response_realization as voice
        from .saved_edit_transport import mutation,calendar_read
        from .provisional_categories import authorize_mapping
        if authorize_mapping(self,service,method,path,body,params):return
        if service=='sheets' and method=='POST' and mutation(self,service,method,path,body,params):return
        if service=='calendar' and method=='GET' and body is None and params is None and calendar_read(self,path):return
        if (self.config.response_realization_enabled and service=='model' and method=='POST' and path=='/v1/responses'
            and params is None and isinstance(body,dict) and body.get('instructions')==voice.INSTRUCTIONS):
            if not voice.validate_body(body) or self.state is None or not self.state.holder:raise LiveDisabled('Unbound response realization')
            fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            for key,run in self.state.model_runs.items():
                if key.startswith('realize01_') and run.status=='reserved' and run.fingerprint==fingerprint and key in self.state.claims:
                    if key in self.model_attempted_events:raise LiveDisabled('Response realization already attempted')
                    self.model_attempted_events.add(key);return
            raise LiveDisabled('No owner-bound realization reservation')
        if service=='calendar':
            from .calendar_transport import authorize
            return authorize(self,method,path,body,params)
        if getattr(self,'raw_history_enabled',False) and service=='firestore' and self.state is not None:
            journal=self.state.raw_journal
            parents={raw.FirestoreJournal(None,self.state.database,self.state.owner_id,book).parent for book in self.config.approved_workbooks}
            if method=='GET' and any(path.startswith('/v1/'+p+'/assistant_raw_messages/') for p in parents) and params is None:return
            if method=='POST' and path in {'/v1/'+p+':runQuery' for p in parents} and params is None:
                q=body.get('structuredQuery',{}) if isinstance(body,dict) else {}
                if q.get('from')==[{'collectionId':'assistant_raw_messages'}] and q.get('limit')==journal.PAGE_SIZE and set(q)<={'from','where','orderBy','limit','startAt'}:return
            if method=='POST' and path=='/v1/'+self.state.database+'/documents:commit' and params is None and self.state.holder:
                writes=body.get('writes',[]) if isinstance(body,dict) else []
                if len(writes)==1 and set(writes[0])=={'update','currentDocument'} and writes[0]['currentDocument']=={'exists':False}:
                    update=writes[0]['update'];fields=update.get('fields',{})
                    if set(fields)=={'at','payload','auth_mac'}:
                        row=raw.valid(json.loads(fields['payload']['stringValue']))
                        if update.get('name')==journal.parent+'/assistant_raw_messages/'+raw.key(row) and fields==journal.fields(row):return
        raw_request=(getattr(self,'raw_history_enabled',False) and service=='model' and method=='POST' and path=='/v1/responses'
                     and params is None and isinstance(body,dict) and body.get('instructions') in {raw.SUMMARY_INSTRUCTIONS,raw.request_instructions(getattr(self.state,'raw_tool_definitions',None) or [],calendar=self.config.calendar_enabled)})
        history=(self.config.history_enabled and service=='model' and method=='POST' and path=='/v1/responses'
                 and params is None and isinstance(body,dict)
                 and body.get('tools') in [tools(enrollment=e,semantic=s,controls=c) for e in [False,True] for s,c in [(False,False),(True,False),(True,True)]]
                 and body.get('instructions') in {INSTRUCTIONS,ENROLLMENT_INSTRUCTIONS,SEMANTIC_INSTRUCTIONS})
        if not history and not raw_request:return super().allowed(service,method,path,body,params)
        keys={'model','instructions','input','tools','tool_choice','parallel_tool_calls','reasoning','max_output_tokens','store','service_tier'}
        if raw_request:
            keys.add('truncation')
            expected=raw.SUMMARY_TOOLS if body['instructions']==raw.SUMMARY_INSTRUCTIONS else tools(enrollment=True,semantic=True,controls=True)
            expected=raw.SUMMARY_TOOLS if body['instructions']==raw.SUMMARY_INSTRUCTIONS else getattr(self.state,'raw_tool_definitions',None)
            if body.get('truncation')!='disabled' or not expected or body['tools']!=expected:raise LiveDisabled('Unbound raw tools')
            raw.reservation(body)
        if (set(body)!=keys or body['model']!=MODEL_ID or body['tool_choice']!=('required' if raw_request and body['instructions']==raw.SUMMARY_INSTRUCTIONS else 'auto') or body['parallel_tool_calls'] is not False
                or body['store'] is not False or body['reasoning']!={'effort':'none'} or body['max_output_tokens']!=(raw.CALENDAR_MAX_OUTPUT_TOKENS if raw_request and self.config.calendar_enabled and body['instructions']!=raw.SUMMARY_INSTRUCTIONS else MAX_OUTPUT_TOKENS)
                or body['service_tier']!='default' or not raw_request and len(json.dumps(body,ensure_ascii=False,separators=(',',':')).encode())>MAX_BODY_BYTES
                or self.state is None or not self.state.holder):raise LiveDisabled('Unbound history request')
        fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
        for key,run in self.state.model_runs.items():
            if run.status=='reserved' and run.fingerprint==fingerprint and key in self.state.claims:
                if key in self.model_attempted_events:raise LiveDisabled('History attempt already sent')
                self.model_attempted_events.add(key);return
        raise LiveDisabled('No owner-bound reservation for history request')

def create_history_cloud_run_app(values=None,*,transport=None,secret_loader=None,clock=None,semantic_enabled=True,raw_history_enabled=True):
    values=values or {};config=HistoryConfig.from_mapping(values)
    if (config.calendar_enabled or config.response_realization_enabled or config.saved_expense_enabled or config.saved_calendar_enabled or config.provisional_categories_enabled) and not raw_history_enabled:raise ConfigError('Calendar/response realization/saved edits/proposals require raw history')
    old=legacy_app({k:v for k,v in values.items() if k not in HISTORY_KEYS},transport=transport,secret_loader=secret_loader,clock=clock)
    if not config.history_state_enabled:return old
    previous=old.state.pilot_http
    http=HistoryHttp(config,previous.secrets,transport=transport,clock=clock);previous.close()
    http.raw_history_enabled=raw_history_enabled
    settings=old.state.assistant.settings
    state=ProductionHistoryState(PilotPort(http,'firestore'),clock=clock);http.state=state
    state.raw_journal.auth_secret=settings.channel_secret
    sheets=SheetsApi(PilotPort(http,'sheets'),config.sheet_target(),effect_guard=state.before_effect)
    guard=DurableModelGuard(state,monthly_micro_usd=config.monthly_micro_usd,max_calls_day=config.max_calls_day,
                            max_calls_month=config.max_calls_month,clock=http.clock,enforce_usage_limits=config.usage_gates_enabled)
    if raw_history_enabled:
        from .response_realization import INSTRUCTIONS as VOICE_INSTRUCTIONS
        guard.reservation_calculator=lambda body:raw.reservation(body) if body.get('instructions') in {raw.request_instructions(getattr(state,'raw_tool_definitions',None) or [],calendar=config.calendar_enabled),raw.SUMMARY_INSTRUCTIONS,VOICE_INSTRUCTIONS} else RESERVATION_MICRO_USD
    legacy=ConversationAssistant(settings,sheets,DisabledCalendar(),ItemCatalog([],{}),state=state,clock=clock,expense_label='ในชีตจริง')
    legacy.production=True;legacy.planner=IntentPlanner(PilotPort(http,'model'),guard,lambda:legacy._active_event_id)
    from .calendar_provider import CalendarCreationApi
    calendar=CalendarCreationApi(PilotPort(http,'calendar'),config.calendar_id,writes_enabled=True,effect_guard=state.before_effect) if config.calendar_enabled else None
    from .expense_edits import SavedExpenseEditor
    from .calendar_reschedule import SavedCalendarEditor
    expense_editor=SavedExpenseEditor(sheets,writes_enabled=config.saved_expense_enabled,single_writer_reviewed=config.saved_expense_concurrency=='reviewed_single_writer')
    calendar_editor=SavedCalendarEditor(PilotPort(http,'calendar'),config.calendar_id,writes_enabled=config.saved_calendar_enabled,effect_guard=state.before_effect) if config.calendar_enabled else None
    assistant=ProductionHistoryAssistant(settings,sheets,port=PilotPort(http,'model'),state=state,clock=clock,history_first_enabled=True,item_enrollment_enabled=True,semantic_enabled=semantic_enabled,raw_history_enabled=raw_history_enabled,calendar_enabled=config.calendar_enabled,calendar_adapter=calendar,calendar_id=config.calendar_id,saved_expense_editor=expense_editor,saved_calendar_editor=calendar_editor,saved_expense_enabled=config.saved_expense_enabled,saved_calendar_enabled=config.saved_calendar_enabled,provisional_categories_enabled=config.provisional_categories_enabled,provisional_mapping_enabled=config.provisional_mapping_enabled,mapping_single_writer_reviewed=config.saved_expense_concurrency=='reviewed_single_writer',historical_categories_enabled=config.historical_categories_enabled)
    assistant.production=True;assistant.expense_label='ในชีตจริง';assistant.guard=guard
    assistant.response_realization_enabled=config.response_realization_enabled
    assistant.bind_legacy(legacy,history_enabled=config.history_enabled)
    replies=BoundedReplies(LineReplyApi(PilotPort(http,'line')),assistant)
    app=create_app(assistant,replies,inspection_mode=False,event_limit=5,body_limit=16384,app_mode='owner_expenses')
    @asynccontextmanager
    async def lifespan(app):
        yield;http.close()
    app.router.lifespan_context=lifespan;app.state.pilot_http=http;app.state.pilot_replies=replies
    return app

def app_factory():
    import os
    return create_history_cloud_run_app({k:os.environ[k] for k in PILOT_KEYS|HISTORY_KEYS if k in os.environ},raw_history_enabled=True)
