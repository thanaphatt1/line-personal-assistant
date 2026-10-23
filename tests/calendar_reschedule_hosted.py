"""One synthetic Calendar supports both event creation and conditional edits."""
from copy import deepcopy
from datetime import datetime
import json
from urllib.parse import unquote
import httpx
from pydantic import SecretStr

from line_assistant.history_cloud_run import create_history_cloud_run_app
from test_deployment_configuration import ConfiguredHosted,TARGET
from test_calendar_factory import values
from test_calendar_mvp import response


class RescheduleHosted(ConfiguredHosted):
    def __init__(self):
        self.patches=0;self.reschedule_enabled=True;self.patch_lost_ack=False;self.patch_race=False
        super().__init__();self.now=datetime.fromisoformat('2026-10-23T13:54:00+07:00')

    def app(self,enabled=True):
        return create_history_cloud_run_app({**values(enabled),**TARGET,
            'PILOT_SAVED_CALENDAR_RESCHEDULE_ENABLED':str(self.reschedule_enabled).lower()},
            transport=httpx.MockTransport(self.handle),secret_loader=lambda name:SecretStr('synthetic-'+name),clock=lambda:self.now)

    def handle(self,request):
        if request.url.host=='www.googleapis.com' and request.method=='PATCH':
            self.patches+=1;identifier=unquote(request.url.path).rsplit('/',1)[-1]
            record=self.calendar.events.get(identifier)
            if record is None:return httpx.Response(404,json={})
            if self.patch_race:record['etag']='"external-version"'
            if request.headers['If-Match']!=record['etag']:return httpx.Response(412,json={})
            assert dict(request.url.params)=={'sendUpdates':'none'}
            body=json.loads(request.content);assert set(body)=={'start','end','extendedProperties'}
            record.update(start=body['start'],end=body['end'],etag='"edit-'+str(self.patches)+'"')
            record.setdefault('extendedProperties',{})['private']=body['extendedProperties']['private']
            if self.patch_lost_ack:raise httpx.ReadTimeout('Synthetic committed reschedule acknowledgement loss',request=request)
            return httpx.Response(200,json=deepcopy(record))
        return super().handle(request)


def context(body):return json.loads(body['input'][0]['content'])


def create(body):
    c=context(body)
    return response('prepare_calendar_event',{'draft_ref':None,'draft_version':None,'title':'กินข้าว',
        'date_text':'4 พย','time_text':'16:00','end_time_text':'22:00','end_date_text':None,'duration_text':None,
        'all_day':False,'source_refs':[c['current_source_ref']]})


def find_last(body):
    c=context(body)
    return response('find_saved_records',{'domain':'calendar','scope':'last','start_date':None,'end_date':None,
        'period_text':None,'name':None,'source_refs':[c['current_source_ref']]})


def move(day='2026-11-14',literal='วันที่ 14'):
    def call(body):
        c=context(body);candidate=c['saved_record_edits']['candidates'][0]
        return response('prepare_calendar_reschedule',{'candidate_ref':candidate['candidate_ref'],
            'date':day,'date_text':literal,'time':None,'time_text':None,'end_date':None,'end_time':None,
            'end_date_text':None,'end_time_text':None,'temporal_refs':[],'source_refs':[c['current_source_ref']]})
    return call
