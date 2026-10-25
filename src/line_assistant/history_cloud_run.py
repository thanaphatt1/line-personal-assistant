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
from .transport import LiveDisabled,NotReady
from .runtime_config import ConfigError
from . import raw_history as raw


HISTORY_KEYS=frozenset({'PILOT_HISTORY_ENABLED','PILOT_NAME_MATCHING_ENABLED','PILOT_ACTIVITY_MEMORY_ENABLED','PILOT_HISTORY_STATE_ENABLED','PILOT_CALENDAR_ENABLED','PILOT_CALENDAR_ID','PILOT_CALENDAR_READ_ALL_ENABLED','PILOT_CALENDAR_READ_IDS','PILOT_CALENDAR_CONNECTIONS_ENABLED','PILOT_RESPONSE_REALIZATION_ENABLED','PILOT_MODEL_USAGE_GATES_ENABLED','PILOT_SAVED_EXPENSE_EDITS_ENABLED','PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED','PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY','PILOT_PROVISIONAL_CATEGORIES_ENABLED','PILOT_PROVISIONAL_MAPPING_ENABLED','PILOT_HISTORICAL_CATEGORIES_ENABLED','PILOT_PREVIOUS_EXPENSE_PRICES_ENABLED','PILOT_TABLE_REGISTRY_JSON','PILOT_SAVED_EXPENSE_DELETE_ENABLED','PILOT_CATALOG_ALIASES_ENABLED','PILOT_RECORD_ACTIONS_ENABLED','PILOT_STATE_RECORDS_ENABLED'})

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
    previous_prices_enabled: bool=False
    calendar_read_all_enabled: bool=False
    calendar_read_ids: tuple[str,...]=()
    calendar_connections_enabled: bool=False
    table_registry_json: str=''
    saved_expense_delete_enabled: bool=False
    catalog_aliases_enabled: bool=False
    record_actions_enabled: bool=False
    state_records_enabled: bool=False
    name_matching_enabled: bool=False
    activity_memory_enabled: bool=False
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
        read_all=values.get('PILOT_CALENDAR_READ_ALL_ENABLED','false')
        if read_all not in {'true','false'} or read_all=='true' and calendar!='true':
            raise ConfigError('All-calendar reads require explicit Calendar/history opt-in')
        from .calendar_provider import additional_calendar_ids
        raw_ids=values.get('PILOT_CALENDAR_READ_IDS','[]')
        try:
            if not isinstance(raw_ids,str) or len(raw_ids)>8192:raise ValueError()
            read_ids=additional_calendar_ids(json.loads(raw_ids),target)
            if read_ids and calendar!='true':raise ValueError()
        except (ValueError,TypeError):raise ConfigError('Additional Calendar reads require a JSON list of explicit IDs and Calendar/history opt-in') from None
        connections=values.get('PILOT_CALENDAR_CONNECTIONS_ENABLED','false')
        if connections not in {'true','false'} or connections=='true' and calendar!='true':raise ConfigError('Calendar connections require explicit Calendar/history opt-in')
        voice=values.get('PILOT_RESPONSE_REALIZATION_ENABLED','false')
        if voice not in {'true','false'} or voice=='true' and (h!='true' or s!='true'):
            raise ConfigError('Response realization requires explicit history/state opt-in')
        usage=values.get('PILOT_MODEL_USAGE_GATES_ENABLED','true')
        if usage not in {'true','false'} or usage=='false' and s!='true':
            raise ConfigError('Observational usage requires explicit single-owner history state')
        expense=values.get('PILOT_SAVED_EXPENSE_EDITS_ENABLED','false');reschedule=values.get('PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED','false')
        concurrency=values.get('PILOT_SAVED_EXPENSE_EDIT_CONCURRENCY','off')
        if (expense not in {'true','false'} or reschedule not in {'true','false'} or concurrency not in {'off','reviewed_single_writer','firestore_transaction'}
            or (expense=='true' or reschedule=='true') and (h!='true' or s!='true')
            or expense=='true' and concurrency!=('firestore_transaction' if core.expense_backend=='firestore' else 'reviewed_single_writer') or reschedule=='true' and calendar!='true'):
            raise ConfigError('Saved-record edits require explicit flags, history and reviewed concurrency policy')
        labels=values.get('PILOT_PROVISIONAL_CATEGORIES_ENABLED','false');mappings=values.get('PILOT_PROVISIONAL_MAPPING_ENABLED','false')
        if (labels not in {'true','false'} or mappings not in {'true','false'} or labels=='true' and (h!='true' or s!='true')
            or mappings=='true' and (labels!='true' or concurrency!=('firestore_transaction' if core.expense_backend=='firestore' else 'reviewed_single_writer'))):raise ConfigError('Provisional mapping requires explicit flags, history and reviewed Database writer policy')
        historical=values.get('PILOT_HISTORICAL_CATEGORIES_ENABLED','false')
        if historical not in {'true','false'} or historical=='true' and labels!='true':raise ConfigError('Historical categories require explicit provisional/history configuration')
        prices=values.get('PILOT_PREVIOUS_EXPENSE_PRICES_ENABLED','false')
        if prices not in {'true','false'} or prices=='true' and (h!='true' or s!='true'):raise ConfigError('Previous expense prices require explicit history/state configuration')
        if core.expense_backend=='firestore' and (h!='true' or s!='true' or not core.launch_authorized):
            raise ConfigError('Firestore expense backend requires the full explicit history composition')
        if concurrency=='firestore_transaction' and core.expense_backend!='firestore':
            raise ConfigError('Transactional concurrency requires Firestore expense backend')
        from .table_storage import parse_registry
        table_json=values.get('PILOT_TABLE_REGISTRY_JSON','')
        try:
            registrations=parse_registry(table_json)
            if registrations and (h!='true' or s!='true'):raise ValueError()
            for registration in registrations:
                binding=registration.storage
                if binding.backend=='sheets' and binding.spreadsheet_id==core.spreadsheet_id and (binding.sheet_id in {core.expense_sheet_id,core.database_sheet_id} or binding.tab in {'All Expenses','Database'}):raise ValueError()
        except ValueError:raise ConfigError('Explicit valid table registry and history/state required; existing expense tabs are reserved') from None
        records=values.get('PILOT_STATE_RECORDS_ENABLED','false')
        if records not in {'true','false'} or records=='true' and (h!='true' or s!='true'):
            raise ConfigError('State records require explicit history/state opt-in')
        extras=[values.get(key,'false') for key in ('PILOT_SAVED_EXPENSE_DELETE_ENABLED','PILOT_CATALOG_ALIASES_ENABLED','PILOT_RECORD_ACTIONS_ENABLED')]
        if any(v not in {'true','false'} for v in extras) or any(v=='true' for v in extras) and (h!='true' or s!='true'):
            raise ConfigError('Owner record features require explicit history/state flags')
        if extras[0]=='true' and expense!='true':raise ConfigError('Expense removal requires saved edits and the reviewed writer policy')
        if extras[2]=='true' and expense!='true':raise ConfigError('Record buttons require saved expense editing')
        names=values.get('PILOT_NAME_MATCHING_ENABLED','false')
        if names not in {'true','false'} or names=='true' and (h!='true' or s!='true'):
            raise ConfigError('Name matching requires explicit history/state opt-in')
        activities=values.get('PILOT_ACTIVITY_MEMORY_ENABLED','false')
        if activities not in {'true','false'} or activities=='true' and (calendar!='true' or voice!='true'):
            raise ConfigError('Activity memory requires Calendar reads and semantic classification')
        return cls(core,h=='true',s=='true',calendar=='true',target or None,voice=='true',usage=='true',expense=='true',reschedule=='true',concurrency,labels=='true',mappings=='true',historical=='true',prices=='true',read_all=='true',read_ids,connections=='true',table_json,*(v=='true' for v in extras),records=='true',names=='true',activities=='true')

class HistoryHttp(PilotHttp):
    def request(self,service,method,path,*,body=None,params=None,headers=None):
        from .loading import OWNER,PATH
        if service=='line' and path==PATH:
            if (not getattr(self,'raw_history_enabled',False) or self.canary or self.reply_check
                    or method!='POST' or params is not None or headers is not None
                    or OWNER.get()!=self.config.owner_id or body!={'chatId':self.config.owner_id,'loadingSeconds':20}):
                raise LiveDisabled('Unbound loading indicator')
            token=self.secrets['line-channel-access-token'].get_secret_value()
            return self._json(self._http(method,'https://api.line.me'+PATH,json=body,headers={'Authorization':'Bearer '+token}))
        from .calendar_provider import CALENDAR_LIST_PATH,calendar_list_page,MAX_READ_CALENDARS
        if service=='calendar' and method=='GET' and path==CALENDAR_LIST_PATH:
            self.allowed(service,method,path,body,params)
            if not params.get('pageToken'):
                from .calendar_connections import connected_ids
                self.calendar_read_ids=frozenset();self.calendar_read_holder=None
                registered=connected_ids(self.state,self.config.owner_id,self.config.calendar_id) if self.config.calendar_connections_enabled else ()
                self.calendar_discovery_ids={self.config.calendar_id,*self.config.calendar_read_ids,*registered};self.calendar_discovery_pages=0
                self.calendar_discovery_tokens=set();self.calendar_discovery_holder=self.state.holder
            try:
                value=super().request(service,method,path,body=body,params=params,headers=headers)
                identifiers,page=calendar_list_page(value)
                self.calendar_discovery_ids.update(identifiers);self.calendar_discovery_pages+=1
                if (len(self.calendar_discovery_ids)>MAX_READ_CALENDARS
                    or page and (page in self.calendar_discovery_tokens or self.calendar_discovery_pages>=3)):
                    raise NotReady('Calendar discovery is incomplete')
                self.calendar_next_page=page
                if page:self.calendar_discovery_tokens.add(page)
                else:
                    self.calendar_read_ids=frozenset(self.calendar_discovery_ids)
                    self.calendar_read_holder=self.state.holder
                return value
            except Exception:
                self.calendar_read_ids=frozenset();self.calendar_read_holder=None;self.calendar_next_page=None
                raise
        if service=='calendar' and method=='PATCH':
            from .saved_edit_transport import mutation
            if not mutation(self,service,method,path,body,params,headers):raise LiveDisabled('Unconfirmed conditional reschedule')
            token=self.google_token('calendar_events')
            return self._json(self._http(method,'https://www.googleapis.com'+path,json=body,params=params,
                headers={'Authorization':'Bearer '+token,**headers}))
        return super().request(service,method,path,body=body,params=params,headers=headers)
    def _json(self,response):
        if not response.is_success:return super()._json(response)
        if getattr(self,'raw_history_enabled',False) and response.request.url.path.endswith((':runQuery',':batchGet',':runAggregationQuery')):
            value=response.json()
            if isinstance(value,list):return value
            raise LiveDisabled('Invalid paginated history response')
        return super()._json(response)
    def allowed(self,service,method,path,body,params):
        records=getattr(self.state,'record_store',None)
        if service=='firestore' and records is not None and records.authorize(method,path,body,params):return
        for table_store in getattr(self,'table_stores',()):
            if service==table_store.binding.backend and table_store.authorize(method,path,body,params):return
        store=getattr(self,'expense_store',None)
        if service=='firestore' and store is not None and store.authorize(method,path,body,params):return
        if service=='sheets' and self.config.expense_backend!='sheets':raise LiveDisabled('Sheets is outside the selected expense backend')
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
        from . import name_matching
        if (self.config.name_matching_enabled and service=='model' and method=='POST' and path=='/v1/responses'
            and params is None and isinstance(body,dict) and body.get('instructions')==name_matching.INSTRUCTIONS):
            if not name_matching.validate_body(body) or self.state is None or not self.state.holder:raise LiveDisabled('Unbound name matching')
            fingerprint=hashlib.sha256(json.dumps(body,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
            for key,run in self.state.model_runs.items():
                if key.startswith(name_matching.PREFIX) and run.status=='reserved' and run.fingerprint==fingerprint and key in self.state.claims:
                    if key in self.model_attempted_events:raise LiveDisabled('Name matching already attempted')
                    self.model_attempted_events.add(key);return
            raise LiveDisabled('No owner-bound name-matching reservation')
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
        expected_choice='required' if raw_request and body['instructions']==raw.SUMMARY_INSTRUCTIONS else 'auto'
        planner_request=raw_request and body['instructions']!=raw.SUMMARY_INSTRUCTIONS
        if planner_request:
            context=json.loads(body['input'][0]['content'])
            if context.get('query_analysis_active'):
                from .query_analysis import READ_TOOLS
                if any(t['name'] not in READ_TOOLS for t in body['tools']):raise LiveDisabled('Unbound analysis tools')
                expected_choice='required'
        expected_effort=raw.PLANNER_REASONING_EFFORT if planner_request else 'none'
        expected_output=raw.PLANNER_MAX_OUTPUT_TOKENS if planner_request else MAX_OUTPUT_TOKENS
        proposal_choice={'type':'function','name':'propose_historical_categories'}
        if raw_request and self.config.historical_categories_enabled and body['instructions']!=raw.SUMMARY_INSTRUCTIONS and body['tool_choice']==proposal_choice:
            pending=self.state.pending if self.state is not None else None
            if not pending or pending.kind!='historical_category_review' or pending.payload.get('stage')!='unclassified':raise LiveDisabled('No historical page awaiting proposals')
            expected_choice=proposal_choice
        if (set(body)!=keys or body['model']!=MODEL_ID or body['tool_choice']!=expected_choice or body['parallel_tool_calls'] is not False
                or body['store'] is not False or body['reasoning']!={'effort':expected_effort} or body['max_output_tokens']!=expected_output
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
    if config.state_records_enabled and not raw_history_enabled:raise ConfigError('State records require original-message history')
    if config.table_registry_json and not raw_history_enabled:raise ConfigError('Tables require original-message history')
    if config.expense_backend=='firestore' and not raw_history_enabled:raise ConfigError('Firestore expenses require original-message history')
    if (config.calendar_enabled or config.response_realization_enabled or config.saved_expense_enabled or config.saved_calendar_enabled or config.provisional_categories_enabled or config.previous_prices_enabled or config.catalog_aliases_enabled or config.record_actions_enabled) and not raw_history_enabled:raise ConfigError('Calendar/response realization/saved edits/proposals require raw history')
    if config.expense_backend=='sheets':
        old=legacy_app({k:v for k,v in values.items() if k not in HISTORY_KEYS},transport=transport,secret_loader=secret_loader,clock=clock)
        if not config.history_state_enabled:return old
        previous=old.state.pilot_http
        http=HistoryHttp(config,previous.secrets,transport=transport,clock=clock);previous.close()
        settings=old.state.assistant.settings
    else:
        from .cloud_run import read_mounted_secret
        from .pilot_config import SECRET_PATHS
        from .config import Settings
        from pydantic import SecretStr
        if transport is None:config.require_live_target()
        loader=secret_loader or read_mounted_secret
        secrets={name:loader(name) for name in SECRET_PATHS}
        if any(type(v) is not SecretStr or not 1<=len(v.get_secret_value())<=8192 or any(not 33<=ord(c)<=126 for c in v.get_secret_value()) for v in secrets.values()):
            raise ConfigError('Complete injected mounted secrets required')
        http=HistoryHttp(config,secrets,transport=transport,clock=clock)
        settings=Settings(owner_id=config.owner_id,channel_secret=secrets['line-channel-secret'].get_secret_value(),mode='production')
    http.raw_history_enabled=raw_history_enabled
    state=ProductionHistoryState(PilotPort(http,'firestore'),clock=clock);http.state=state
    state.raw_journal.auth_secret=settings.channel_secret
    if config.expense_backend=='firestore':
        from .firestore_expenses import FirestoreExpenseStore
        sheets=FirestoreExpenseStore(PilotPort(http,'firestore'),config.firestore_expense_target(),state=state,effect_guard=state.before_effect)
        http.expense_store=sheets
    else:
        sheets=SheetsApi(PilotPort(http,'sheets'),config.sheet_target(),effect_guard=state.before_effect)
    guard=DurableModelGuard(state,monthly_micro_usd=config.monthly_micro_usd,max_calls_day=config.max_calls_day,
                            max_calls_month=config.max_calls_month,clock=http.clock,enforce_usage_limits=config.usage_gates_enabled)
    if raw_history_enabled:
        from .response_realization import INSTRUCTIONS as VOICE_INSTRUCTIONS
        from .name_matching import INSTRUCTIONS as NAME_INSTRUCTIONS
        guard.reservation_calculator=lambda body:raw.reservation(body) if body.get('instructions') in {raw.request_instructions(getattr(state,'raw_tool_definitions',None) or [],calendar=config.calendar_enabled),raw.SUMMARY_INSTRUCTIONS,VOICE_INSTRUCTIONS,NAME_INSTRUCTIONS} else RESERVATION_MICRO_USD
    legacy=ConversationAssistant(settings,sheets,DisabledCalendar(),ItemCatalog([],{}),state=state,clock=clock,expense_label='ในฐานข้อมูลรายจ่าย' if config.expense_backend=='firestore' else 'ในชีตจริง')
    legacy.production=True;legacy.planner=IntentPlanner(PilotPort(http,'model'),guard,lambda:legacy._active_event_id)
    from .calendar_provider import CalendarCreationApi
    from .calendar_connections import connected_ids
    calendar=CalendarCreationApi(PilotPort(http,'calendar'),config.calendar_id,writes_enabled=True,effect_guard=state.before_effect,read_all_enabled=config.calendar_read_all_enabled,calendar_read_ids=config.calendar_read_ids,
        connected_ids=lambda:connected_ids(state,config.owner_id,config.calendar_id) if config.calendar_connections_enabled else ()) if config.calendar_enabled else None
    from .expense_edits import SavedExpenseEditor
    from .calendar_reschedule import SavedCalendarEditor
    if config.expense_backend=='firestore':
        from .firestore_expenses import FirestoreSavedExpenseEditor
        expense_editor=FirestoreSavedExpenseEditor(sheets,writes_enabled=config.saved_expense_enabled,delete_enabled=config.saved_expense_delete_enabled)
    else:
        expense_editor=SavedExpenseEditor(sheets,writes_enabled=config.saved_expense_enabled,single_writer_reviewed=config.saved_expense_concurrency=='reviewed_single_writer',delete_enabled=config.saved_expense_delete_enabled)
    calendar_editor=SavedCalendarEditor(PilotPort(http,'calendar'),config.calendar_id,writes_enabled=config.saved_calendar_enabled,effect_guard=state.before_effect) if config.calendar_enabled else None
    from .table_storage import parse_registry,build_registry
    table_registry=build_registry(parse_registry(config.table_registry_json),clients={name:PilotPort(http,name) for name in ('sheets','firestore')},owner_id=config.owner_id,project_id=config.project_id,state=state)
    http.table_stores=tuple(table_registry.stores.values())
    assistant=ProductionHistoryAssistant(settings,sheets,port=PilotPort(http,'model'),state=state,clock=clock,history_first_enabled=True,item_enrollment_enabled=True,semantic_enabled=semantic_enabled,raw_history_enabled=raw_history_enabled,calendar_enabled=config.calendar_enabled,calendar_adapter=calendar,calendar_id=config.calendar_id,saved_expense_editor=expense_editor,saved_calendar_editor=calendar_editor,saved_expense_enabled=config.saved_expense_enabled,saved_calendar_enabled=config.saved_calendar_enabled,provisional_categories_enabled=config.provisional_categories_enabled,provisional_mapping_enabled=config.provisional_mapping_enabled,mapping_single_writer_reviewed=config.saved_expense_concurrency=='reviewed_single_writer',historical_categories_enabled=config.historical_categories_enabled,previous_prices_enabled=config.previous_prices_enabled,calendar_connections_enabled=config.calendar_connections_enabled,calendar_sharing_email=config.service_account)
    assistant.production=True;assistant.expense_label='ในฐานข้อมูลรายจ่าย' if config.expense_backend=='firestore' else 'ในชีตจริง';assistant.guard=guard
    assistant.saved_edits.delete_enabled=config.saved_expense_delete_enabled
    assistant.catalog_aliases.enabled=config.catalog_aliases_enabled
    assistant.record_actions.enabled=config.record_actions_enabled
    from .table_workflow import TableWorkflow
    assistant.table_flow=TableWorkflow(assistant,table_registry)
    assistant.response_realization_enabled=config.response_realization_enabled
    assistant.name_matching_enabled=config.name_matching_enabled and raw_history_enabled
    assistant.activity_memory_enabled=config.activity_memory_enabled and raw_history_enabled
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
