"""Computed results can support a concise explanation without copying every row.

The model choices here are scripted; arithmetic/effects and delivered bindings
are asserted independently, not treated as fresh language-understanding proof.
"""
from copy import deepcopy
from datetime import datetime
from decimal import Decimal
import json
import socket

import pytest

from line_assistant import answer_composition as ac,operation_composition as oc,response_realization as voice
from line_assistant.contracts import Transaction
from test_calendar_mvp import response
from test_intent_actions import harness,read


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def source(start='2026-01-01',end='2026-12-31',mode='calendar_year'):
    return {'source':'expense','alias':'e','parameters':{'date_mode':mode,'start_date':start,
        'end_date':end,'relative_offset':None,'period_text':'ช่วงที่ต้องการ'}}


def answer(template):
    def choose(body):
        context=json.loads(body['input'][0]['content']);offered=context['read_responses'][-1]
        return response('act',{'request':{'intent':'respond','response_ref':offered['response_ref'],
            'document':{'blocks':[{'kind':'text','template':template(offered['facts']) if callable(template) else template}]}}})
    return choose


def fixture():
    h=harness();h.now=datetime.fromisoformat('2026-11-05T11:27:00+07:00')
    h.sheets.rows=[('Service Alpha','Other',''),('Service Beta','Other',''),('Service Gamma','Other','')]
    h.sheets.entries=[Transaction(item=name,amount=amount,kind='expense',date=day)
        for name,amount,day in [('Service Alpha','300','2026-09-01'),('Service Beta','200','2026-09-02'),
                              ('Service Gamma','100','2026-09-03'),('Service Gamma','50','2026-05-01'),
                              ('Service Gamma','25','2026-11-01')]]
    return h


def test_maximum_month_can_be_a_sentence_with_a_bound_readable_month_and_amount():
    h=fixture();before=deepcopy(h.sheets.entries)
    # Aggregate the whole year before selecting the winning month, retaining ties.
    sql="WITH monthly AS (SELECT strftime('%Y-%m',date) AS month,SUM(amount) AS amount FROM e GROUP BY 1) SELECT month,amount FROM monthly WHERE amount=(SELECT MAX(amount COLLATE decimal) FROM monthly)"
    r=h.send('เดือนไหนใช้เงินเยอะสุดในปีนี้',read(sql,sources=[source()]),
             answer('ใน{{f1}} เดือนที่ใช้เงินสูงสุดคือ{{r1_c1}} รวม {{r1_c2}}ครับ'))
    assert r.status=='answer' and r.data['response_realization']['status']=='llm'
    assert r.message=='ในปี 2026 เดือนที่ใช้เงินสูงสุดคือกันยายน 2026 รวม 600 บาทครับ'
    raw=next(v for v in h.app._query_reads.values() if v.get('relational_query'));assert raw['rows']==[{'month':'2026-09','amount':'600'}]
    assert h.sheets.entries==before and len(h.model.requests)==2 and not h.sheets.write_log and not h.state.actions


def test_summary_can_cite_winner_without_dumping_other_computed_months_and_keep_followup():
    h=fixture();reads=[];saved=h.sheets.saved_entries
    h.sheets.saved_entries=lambda *args:(reads.append(args) or saved(*args))
    ranking="SELECT strftime('%Y-%m',date) AS month,SUM(amount) AS amount FROM e GROUP BY 1 ORDER BY amount COLLATE decimal DESC"
    first=h.send('เดือนไหนใช้เยอะสุด',read(ranking,sources=[source()]),answer('{{f1}} ใช้สูงสุดใน{{r1_c1}} รวม {{r1_c2}}ครับ'))
    assert 'กันยายน 2026' in first.message and 'พฤษภาคม' not in first.message and '25 บาท' not in first.message
    first_raw=next(v for v in h.app._query_reads.values() if v.get('relational_query'))
    assert first_raw['count']==3 and len(first_raw['rows'])==3
    sql=("WITH by_item AS (SELECT item,SUM(amount) AS amount,COUNT(*) AS payments FROM e GROUP BY item) "
         "SELECT item,amount,payments,SUM(amount) OVER () AS month_total,"
         "decimal_div(decimal_mul(amount,'100'),SUM(amount) OVER (),2) AS share_pct "
         "FROM by_item ORDER BY amount COLLATE decimal DESC")
    def breakdown(body):
        context=json.loads(body['input'][0]['content'])
        assert context['previous_query']['answer_context']['records'][0]['month']=='2026-09'
        return read(sql,sources=[source('2026-09-01','2026-09-30','calendar_month')])(body)
    template=('{{f1}} มีรายจ่ายรวม {{r1_c4}}ครับ รายการหลักคือ {{r1_c1}} {{r1_c2}} '
              'คิดเป็น {{r1_c5}}% ของเดือน รองลงมาคือ {{r2_c1}} {{r2_c2}} '
              'เป็นรายการที่มีสัดส่วนสูงในข้อมูลที่บันทึกไว้ครับ')
    h.model.values=[breakdown,answer(template)];event=h.event('ทำไมเดือนนั้นใช้เยอะจัง')
    second=h.app.handle(event)
    assert second.data['response_realization']['status']=='llm' and second.status=='answer'
    assert second.message.startswith('เดือนกันยายน 2026 มีรายจ่ายรวม 600 บาทครับ')
    assert 'Service Alpha 300 บาท' in second.message and '50.00%' in second.message
    assert 'Service Beta 200 บาท' in second.message and 'Service Gamma' not in second.message
    assert 'payment' not in second.message and 'total' not in second.message and '|' not in second.message
    rows=next(v for v in h.app._query_reads.values() if v.get('relational_query'))['rows'];total=sum(Decimal(v['amount']) for v in rows)
    assert total==Decimal('600') and Decimal(rows[0]['share_pct'])==Decimal(rows[0]['amount'])*100/total
    assert len(rows)==3 and len(reads)==2 and len(h.model.requests)==4
    assert not h.sheets.write_log and not h.state.actions and not h.provider.posts
    calls=len(h.model.requests);read_count=len(reads)
    assert h.app.handle(deepcopy(event))==second
    assert len(h.model.requests)==calls and len(reads)==read_count and not h.sheets.write_log


def view():
    value=ac.View(selective=True);value.fact('ช่วงข้อมูลที่ตรวจแล้ว')
    value.fact('ข้อมูลบางส่วนจากผลลัพธ์ที่คำนวณครบแล้ว','coverage')
    for name,amount,count in [('Alpha','300',3),('Beta','200',2),('Gamma','100',1)]:
        value.row([('ชื่อ',name,None,None),('ยอด',amount,None,'บาท'),('จำนวน',count,None,'ครั้ง')])
    return value.data()


def test_selective_summary_retains_required_scope_and_coverage_but_can_omit_rows_and_cells():
    data=view();copy=deepcopy(data)
    doc={'blocks':[{'kind':'text','template':'{{f1}} รายการหลักคือ {{r1_c1}} {{r1_c2}} และ {{r2_c1}} {{r2_c2}}ครับ {{f2}}'}]}
    text=ac.render(doc,data)
    assert 'Alpha 300 บาท' in text and 'Beta 200 บาท' in text and 'Gamma' not in text and 'ครั้ง' not in text
    assert data==copy


@pytest.mark.parametrize('template',[
    '{{r1_c1}} {{r1_c2}} {{f2}}',  # missing scope
    '{{f1}} {{r1_c1}} {{r1_c2}}',  # missing coverage qualifier
    '{{f1}} {{f2}} {{r99_c1}}',
    '{{f1}} {{f2}} {{c1}}',  # ambiguous multi-record cell
    '{{f1}} {{f2}} เพิ่มอีก 999 บาท',
    '{{f1}} {{f2}}',  # no result cell at all
])
def test_summary_cannot_drop_mandatory_context_or_fabricate_business_values(template):
    with pytest.raises(ac.AnswerError):ac.render({'blocks':[{'kind':'text','template':template}]},view())


def test_empty_rows_cannot_satisfy_result_evidence():
    doc={'blocks':[{'kind':'text','template':'{{f1}} {{f2}}'},
                   {'kind':'rows','record_refs':['r1'],'heading':'','template':''}]}
    with pytest.raises(ac.AnswerError):ac.render(doc,view())


def test_read_selection_policy_cannot_be_used_to_drop_operation_review_details():
    data=view();doc={'blocks':[{'kind':'text','template':'{{f1}} {{f2}} {{r1_c1}}'}]}
    with pytest.raises(ValueError):oc.render(doc,data)
    strict=deepcopy(data);strict.pop('record_policy')
    with pytest.raises(ac.AnswerError):ac.render(doc,strict)


def test_compact_group_identity_preserves_shared_context_without_clipping_collisions():
    value=ac.View()
    for name in ['x'*301+'A','x'*301+'B','x'*301+'A']:
        value.row([('name',name,None,None)])
    first,second,third=[row['fields']['c1'] for row in value.records]
    assert first['display']==second['display'] and first['group_key']!=second['group_key']
    assert first['group_key']==third['group_key'] and len(first['group_key'])<8


def test_grouped_read_context_fits_existing_composer_bound_and_keeps_all_result_values():
    h=fixture();h.sheets.rows=[('บริการจำลองเพื่อการทดสอบ '+str(i),'Other','') for i in range(39)]
    h.sheets.entries=[Transaction(item=name,amount=str(100+i),kind='expense',date='2026-09-10') for i,(name,*_) in enumerate(h.sheets.rows)]
    sql='SELECT item,COUNT(*) AS payment_count,SUM(amount) AS total_thb FROM e GROUP BY item ORDER BY total_thb COLLATE decimal DESC'
    def brief(facts):return 'รายการหลักใน{{f1}} คือ {{r1_c1}} {{r1_c3}}ครับ {{f2}}'
    r=h.send('ทำไมเดือนกันยายนใช้เยอะ',read(sql,sources=[source('2026-09-01','2026-09-30','calendar_month')]),answer(brief))
    raw=next(v for v in h.app._query_reads.values() if v.get('relational_query'))
    assert r.data['response_realization']['status']=='llm' and raw['count']==39
    assert len(raw['rows'])==20 and 'ข้อมูลบางส่วน' in r.message
    body=voice.body_for(h.app,r)
    assert body is not None and voice.validate_body(body) and len(voice.encoded(body))<=24000
    assert len(h.model.requests)==2 and not h.sheets.write_log and not h.provider.posts


def test_maximum_ties_can_be_reported_together_without_other_months():
    h=fixture();h.sheets.entries.append(Transaction(item='Service Alpha',amount='575',kind='expense',date='2026-11-02'))
    sql="WITH monthly AS (SELECT strftime('%Y-%m',date) AS month,SUM(amount) AS amount FROM e GROUP BY 1) SELECT month,amount FROM monthly WHERE amount=(SELECT MAX(amount COLLATE decimal) FROM monthly) ORDER BY month"
    def tied(body):
        c=json.loads(body['input'][0]['content']);offered=c['read_responses'][-1]
        return response('act',{'request':{'intent':'respond','response_ref':offered['response_ref'],'document':{'blocks':[
            {'kind':'text','template':'{{f1}} มีเดือนที่รายจ่ายสูงสุดเท่ากันครับ'},
            {'kind':'rows','record_refs':['r1','r2'],'heading':'','template':'{{c1}} รวม {{c2}}'}]}}})
    r=h.send('เดือนไหนสูงสุด',read(sql,sources=[source()]),tied)
    assert r.data['response_realization']['status']=='llm'
    assert 'กันยายน 2026' in r.message and 'พฤศจิกายน 2026' in r.message and r.message.count('600 บาท')==2
    assert 'พฤษภาคม' not in r.message and not h.sheets.write_log


def test_requested_detailed_output_can_still_include_all_computed_rows_and_cells():
    from test_intent_actions import respond
    h=fixture()
    r=h.send('ขอรายละเอียดทุกเดือน',read("SELECT strftime('%Y-%m',date) AS month,SUM(amount) AS amount FROM e GROUP BY 1 ORDER BY month",sources=[source()]),respond())
    assert r.data['response_realization']['status']=='llm'
    assert all(month in r.message for month in ['พฤษภาคม 2026','กันยายน 2026','พฤศจิกายน 2026'])
    assert '50 บาท' in r.message and '600 บาท' in r.message and '25 บาท' in r.message
    assert len(h.model.requests)==2 and not h.sheets.write_log and not h.state.actions
