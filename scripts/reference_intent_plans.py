"""Scripted controls for harness calibration, never model accuracy evidence."""
import json
from test_calendar_mvp import response
from test_response_realization import generated
from line_assistant import response_realization


class ReferenceModel:
    def __init__(self,plans):self.plans=plans;self.index=-1;self.source_ref=None

    def __call__(self,body):
        c=json.loads(body['input'][0]['content'])
        if body.get('instructions')==response_realization.INSTRUCTIONS:return generated(body)
        if c['current_source_ref']!=self.source_ref:
            self.source_ref=c['current_source_ref'];self.index+=1
        plan=self.plans[self.index]
        if plan['kind']=='clarify':return response('finish_query_analysis',{'query_refs':[],'operation':'clarify','question':plan['text'],
            'intent_review':{'selection_matches':True,'output_matches':True,'unresolved_conditions':['period']},'source_refs':[c['current_source_ref']]})
        if plan['kind']=='prepare_calendar':
            return response('prepare_calendar_event',{'draft_ref':None,'draft_version':None,
                **{k:plan.get(k) for k in ['title','date_text','time_text','end_date_text','end_time_text','duration_text']},
                'source_refs':[c['current_source_ref']]})
        if plan['kind']!='read':raise AssertionError('Unexpected model call in a deterministic confirmation/replay')
        if c.get('read_responses'):
            offered=c['read_responses'][-1];view=offered['facts']
            document={'blocks':[
                *[{'kind':'text','template':'{{'+key+'}}'} for key in view['facts']],
                *[{'kind':'rows','record_refs':[row['id']],'heading':'','template':' | '.join('{{'+key+'}}' for key in row['fields'])} for row in view['records']]]}
            return response('act',{'request':{'intent':'respond','response_ref':offered['response_ref'],
                'review':{'selection_matches':True,'output_matches':True,'unresolved_conditions':[]},
                'document':document,'source_refs':[c['current_source_ref']]}})
        return response('act',{'request':{'intent':'read','sources':plan['sources'],'sql':plan['sql'],'source_refs':[c['current_source_ref']]}})
