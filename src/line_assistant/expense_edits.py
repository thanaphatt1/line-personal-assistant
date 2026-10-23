"""Bounded saved-expense edits by stable ID; no credential discovery.

Sheets batchUpdate is atomic as a batch, not a compare-and-swap against outside
collaborators. Live enablement requires an explicitly reviewed single-writer
policy. Owner lease, fresh ID/row fingerprint and readback are still mandatory.
"""
from copy import deepcopy
from datetime import date
from decimal import Decimal, InvalidOperation
import json,re
from .adapters import Conflict,NotFound,Reconciliation
from .contracts import Transaction
from .history_context import digest
from .sheet_dates import date_to_serial,serial_to_date
from .sheets_api import normalized_row,tab_range
from .transport import ApiError,NotReady,OutcomeUnknown,WriteBlocked


def expense_after(before,changes):
    if not isinstance(changes,dict) or not changes or set(changes)-{'item','amount','date'}:
        raise ValueError('saved_expense_patch_fields')
    values={k:before[k] for k in ['item','amount','date']}
    proposal=Transaction.model_validate({**values,**changes,'kind':'expense'})
    after=deepcopy(values)
    for field in changes:
        value=getattr(proposal,field)
        after[field]=value.isoformat() if field=='date' else f'{Decimal(value):.2f}' if field=='amount' else value
    if after==values:raise ValueError('saved_expense_no_change')
    return after


def edit_body(target,payload,row):
    before,after=payload['before'],payload['after']
    if type(row) is not int or not 2<=row<=target.max_rows:raise ValueError('saved_expense_row_bound')
    if payload.get('operation')=='delete':
        if after is not None or payload.get('changes')!={'operation':'delete'} or payload['id']!=before['id']:
            raise ValueError('saved_expense_delete_target')
        # Keep the stable ID in E as positive removal evidence, preserve all
        # other columns/formatting, and never shift another expense's row.
        return {'requests':[{'updateCells':{'start':{'sheetId':target.expense_sheet_id,'rowIndex':row-1,'columnIndex':0},
            'rows':[{'values':[{'userEnteredValue':{'stringValue':''}} for _ in range(4)]}],'fields':'userEnteredValue'}}],
            'includeSpreadsheetInResponse':True,'responseRanges':[tab_range(target.expense_tab,f'A{row}:E{row}')],
            'responseIncludeGridData':True}
    if set(after)!={'item','amount','date'} or payload['id']!=before['id']:raise ValueError('saved_expense_target')
    requests=[]
    for field,column in [('date',0),('item',1),('amount',2)]:
        if before[field]==after[field]:continue
        cell={'stringValue':after[field]} if field=='item' else {'numberValue':date_to_serial(date.fromisoformat(after[field])) if field=='date' else float(Decimal(after[field]))}
        requests.append({'updateCells':{'start':{'sheetId':target.expense_sheet_id,'rowIndex':row-1,'columnIndex':column},
            'rows':[{'values':[{'userEnteredValue':cell}]}],'fields':'userEnteredValue'}})
    if not requests:raise ValueError('saved_expense_no_change')
    return {'requests':requests,'includeSpreadsheetInResponse':True,
        'responseRanges':[tab_range(target.expense_tab,f'A{row}:E{row}')],'responseIncludeGridData':True}


class SavedExpenseEditor:
    def __init__(self,sheets,*,writes_enabled=False,single_writer_reviewed=False,delete_enabled=False):
        if any(type(v) is not bool for v in (writes_enabled,single_writer_reviewed,delete_enabled)):raise ValueError('explicit_saved_edit_flags')
        if writes_enabled and not single_writer_reviewed:raise ValueError('saved_expense_concurrency_policy_required')
        self.sheets=sheets;self.client=sheets.client;self.target=sheets.target;self.base=sheets.base
        self.writes_enabled=writes_enabled;self.single_writer_reviewed=single_writer_reviewed;self.mocked=self.client.mocked
        self.delete_enabled=delete_enabled

    def records(self,start=None,end=None,*,ids=None):
        metadata=self.target.transaction_id_metadata
        if not metadata or metadata.index!=5 or metadata.header!='transaction_id':raise NotReady('Stable existing transaction_id E is required')
        grid=self.sheets.metadata()[self.target.expense_tab]['properties']['gridProperties']
        if type(grid.get('rowCount')) is not int or not 1<=grid['rowCount']<=self.target.max_rows or grid.get('columnCount',0)<5:
            raise NotReady('Saved expense lookup exceeds reviewed grid')
        rows=self.sheets.values(self.target.expense_tab,f'A1:E{grid["rowCount"]}',formulas=True)
        if not rows or rows[0]!=['Date','List','Cost','Remark','transaction_id'] or len(rows)>grid['rowCount']:
            raise NotReady('Saved expense header/grid drift')
        requested=set(ids) if ids is not None else None
        if requested is not None and (not 1<=len(requested)<=10 or any(not isinstance(v,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',v) for v in requested)):
            raise ValueError('saved_expense_id_bound')
        counts={}
        for raw in rows[1:]:
            if len(raw)>=5 and isinstance(raw[4],str) and raw[4]:counts[raw[4]]=counts.get(raw[4],0)+1
        if any(n>1 for value,n in counts.items() if requested is None or value in requested):raise Conflict('Duplicate saved transaction ID')
        result=[];unidentified=0;removed=[]
        for index,raw in enumerate(rows[1:],2):
            if not any(v not in ('',None) for v in raw):continue
            values=normalized_row(raw,5)
            if requested is not None and (not isinstance(values[4],str) or values[4] not in requested):continue
            if (all(v in ('',None) for v in values[:4]) and isinstance(values[4],str)
                and re.fullmatch(r'[A-Za-z0-9_-]{1,128}',values[4])):
                removed.append({'id':values[4],'row':index,'raw':values,'workbook':self.target.spreadsheet_id});continue
            try:
                if len(values)!=5 or type(values[0]) not in {int,float} or int(values[0])!=values[0]:raise ValueError()
                if not isinstance(values[1],str) or not 1<=len(values[1])<=200 or type(values[2]) not in {int,float}:raise ValueError()
                day=serial_to_date(int(values[0]));amount=Decimal(str(values[2]))
                if not amount.is_finite() or amount<=0 or amount.quantize(Decimal('.01'))!=amount:raise ValueError()
                if not isinstance(values[3],str) or len(values[3])>500:raise ValueError()
            except (ValueError,OverflowError,InvalidOperation):raise NotReady('Saved expense inputs/formulas require manual review') from None
            if requested is None and (start is None or end is None or not start<=day<=end):continue
            identifier=values[4]
            if not isinstance(identifier,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,128}',identifier):unidentified+=1;continue
            record={'id':identifier,'row':index,'item':values[1],'amount':f'{amount:.2f}','date':day.isoformat(),
                'raw':values,'workbook':self.target.spreadsheet_id}
            record['version']=digest({k:record[k] for k in ['id','raw','workbook']})
            result.append(record)
        return {'records':result,'unidentified_count':unidentified,'complete':True,'removed':removed}

    def get(self,identifier):
        rows=self.records(ids=[identifier])['records']
        if len(rows)!=1:raise NotFound('Saved transaction ID is missing')
        return rows[0]

    def _same_after(self,row,payload):
        return row['id']==payload['id'] and all(row[k]==payload['after'][k] for k in ['item','amount','date']) and row['raw'][3:]==payload['before']['raw'][3:]

    def apply(self,payload,key,*,bind_request):
        if not self.writes_enabled or not self.single_writer_reviewed or not self.target.writes_enabled or not self.target.input_access_verified:
            raise WriteBlocked('Saved expense editing is disabled or concurrency policy unreviewed')
        if payload.get('domain')!='expense' or not key:raise WriteBlocked('Bound saved expense action required')
        deleting=payload.get('operation')=='delete'
        if deleting and not self.delete_enabled:raise WriteBlocked('Saved expense removal is disabled')
        current=self.get(payload['id'])
        if current['version']!=payload['before']['version']:raise Conflict('Saved expense changed after review')
        # Enforced range protections remain fail-closed, as for identified appends.
        props=self.sheets.metadata()[self.target.expense_tab]
        if any(not v.get('warningOnly',False) for v in props.get('protectedRanges',[])):
            raise WriteBlocked('Saved expense protections need explicit review')
        body=edit_body(self.target,payload,current['row'])
        bind_request(body,{'row':current['row'],'id':current['id'],'version':current['version']})
        self.sheets.effect_guard()
        try:response=self.client.request('POST',self.base+':batchUpdate',body=body)
        except ApiError as error:
            if error.status in {401,403}:raise WriteBlocked('Existing Sheets edit permission is unavailable') from None
            if error.status==404:raise NotFound('Configured expense workbook is missing') from None
            raise
        if response.get('spreadsheetId')!=self.target.spreadsheet_id or len(response.get('replies',[]))!=len(body['requests']):
            raise OutcomeUnknown('Saved expense acknowledgement target/count changed')
        # The returned grid also proves which stable ID occupied that row when
        # the batch executed. A mismatch is never treated as successful.
        cells=[]
        for sheet in response.get('updatedSpreadsheet',{}).get('sheets',[]):
            if sheet.get('properties',{}).get('sheetId')!=self.target.expense_sheet_id:continue
            for data in sheet.get('data',[]):
                if data.get('startRow',0)==current['row']-1 and data.get('startColumn',0)==0:
                    if data.get('rowData'):cells=data['rowData'][0].get('values',[])
        if len(cells)<5 or cells[4].get('userEnteredValue',{}).get('stringValue')!=payload['id']:
            raise OutcomeUnknown('Saved expense stable ID at mutation is unproven')
        try:
            if deleting:
                removed=self.records(ids=[payload['id']])['removed']
                if len(removed)!=1:raise ValueError('Removal not observed')
                return {'id':payload['id'],'row':removed[0]['row'],
                    'before':{k:payload['before'][k] for k in ['item','amount','date']},'after':None,'proof':'ack_and_readback'}
            after=self.get(payload['id'])
        except Exception:raise OutcomeUnknown('Saved expense write occurred but readback is unavailable') from None
        if not self._same_after(after,payload):raise OutcomeUnknown('Saved expense readback does not match confirmed values')
        return {'id':after['id'],'row':after['row'],'before':{k:payload['before'][k] for k in ['item','amount','date']},
            'after':{k:after[k] for k in ['item','amount','date']},'proof':'ack_and_readback'}

    def reconcile(self,key,payload):
        if payload.get('operation')=='delete':
            try:removed=self.records(ids=[payload['id']])['removed']
            except Exception:return Reconciliation('unknown')
            if len(removed)==1:
                return Reconciliation('committed',{'id':payload['id'],'row':removed[0]['row'],
                    'before':{k:payload['before'][k] for k in ['item','amount','date']},'after':None,'proof':'observed_removed_state'})
            return Reconciliation('unknown')
        try:row=self.get(payload['id'])
        except Exception:return Reconciliation('unknown')
        if self._same_after(row,payload):
            return Reconciliation('committed',{'id':row['id'],'row':row['row'],'before':{k:payload['before'][k] for k in ['item','amount','date']},
                'after':{k:row[k] for k in ['item','amount','date']},'proof':'observed_desired_state'})
        # An old-looking value after timeout does not prove an in-flight
        # write cannot still arrive. Never automatically retry an edit.
        return Reconciliation('unknown')
