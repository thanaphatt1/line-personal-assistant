"""Return actionable planning failures to the same model, without write tools.

Validators declare recoverability. Unknown provider faults, missing owner
input and unsupported actions terminate. Repeated plans/diagnostics stop;
there is no fixed attempt count. No transport or mutation is retried.
"""
import json
import re
from .history_context import digest


def diagnostic_fields(schemas,pending,h):
    names=set()
    def walk(value):
        if isinstance(value,dict):
            names.update(value.get('properties',{}))
            for child in value.values():walk(child)
        elif isinstance(value,list):
            for child in value:walk(child)
    walk(schemas)
    names.update(f['name'] for view in pending for record in view['records'] for f in record['fields'])
    registry=getattr(getattr(h,'table_flow',None),'registry',None)
    if registry:names.update(c.name for table in registry.tables.values() for c in table.columns)
    return names

class RepairLoop:
    def __init__(self,h):self.h=h;self.plans=set();self.failures={};self.attempts=0

    def feedback(self,call,result):
        if result.get('ok') is not False:return None
        error=result.get('error') or result.get('error_code')
        metadata=result.get('recovery',{})
        if metadata.get('kind')!='assistant_arguments' or metadata.get('handled_by'):return None
        name=call['name']
        # Multi-source analysis already returns each failed read to its planner
        # and retains it as evidence of the original goal. Do not intercept it
        # before that pipeline records the failed expense/calendar read.
        from .query_analysis import READ_TOOLS
        if name in READ_TOOLS and self.h._query_analysis_active and not (result.get('temporal_resolution') or result.get('relational_query') or result.get('analysis_selection_required')):return None
        try:args=json.loads(call['arguments'])
        except (ValueError,TypeError):return None
        from .pending_records import views
        pending=views(self.h)
        target=args.get('draft_ref') or args.get('table_id')
        offered={t['name']:t for t in self.h.state.raw_tool_definitions}
        allowed=sorted(({name}|set(metadata.get('allowed_tools',())))&offered.keys())
        if not allowed:return None
        schemas={key:offered[key]['parameters'] for key in allowed}
        known=diagnostic_fields(schemas,pending,self.h)
        def field(value):
            parts=re.findall(r'[a-zA-Z_][a-zA-Z_0-9]*',str(value))
            return str(value) if parts and all(v in known for v in parts) else '<unknown_field>'
        fields=[field(v) for v in metadata.get('invalid_fields',[])]
        prefix,_,suffix=error.partition(':') if isinstance(error,str) else ('unknown','','')
        if suffix:fields.append(field(suffix))
        targets={v['draft_ref'] for v in pending}
        registry=getattr(getattr(self.h,'table_flow',None),'registry',None)
        if registry:targets.update(registry.tables)
        target=target if target is None or target in targets else '<unoffered_target>'
        # A final selection can become repairable again after a new verified
        # result appears. Repeating it against unchanged evidence still stops.
        progress=sorted(ref for ref,row in getattr(self.h,'_query_reads',{}).items()
            if row.get('ok') and not row.get('read_failure')) if result.get('analysis_selection_required') else []
        plan=digest([name,args,progress]);failure=digest([name,prefix,target,fields,metadata.get('required_inputs',[]),schemas,progress])
        self.failures[failure]=self.failures.get(failure,0)+1
        if plan in self.plans or self.failures[failure]>1:
            result['repair_stopped']='repeated_failure'
            return None
        self.plans.add(plan);self.attempts+=1
        from .action_trace import emit,code
        emit('planning_repair',tool=code(name),error=code(error.split(':',1)[0] if isinstance(error,str) else None),attempt=self.attempts)
        return {**result,'repair_feedback':{'kind':'assistant_arguments','error_code':error,
            'invalid_fields':metadata.get('invalid_fields',[]),'required_inputs':metadata.get('required_inputs',[]),
            'pending_records':pending,'schemas':schemas,'allowed_tools':allowed,
            'instruction':'Correct only this current requested plan using the tool-declared schema and verified OWNER evidence. resolve_temporal may compute dates/times from a declared semantic plan and verified bases; never invent factual inputs. No new task, confirmation, provider retry or write. Ask one specific question when a required fact or selection is genuinely missing. Stop repeating unchanged plans or diagnostics.'}}
