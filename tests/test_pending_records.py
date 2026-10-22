"""General pending-record patches; synthetic owner facts and services only."""
from copy import deepcopy
import json,socket
import pytest

from line_assistant.pending_records import NAME
from line_assistant.history_context import digest
from test_weekday_enrollment_v11 import harness,active,CATEGORIES
from test_catalog_names_v09 import prepare,entry
from test_history_first import response

@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic adapters only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def pending():
    h=harness()
    prepare(h,'ขนมทดสอบไลรา 47 บาท เครื่องดื่มทดสอบโนวา 53 บาท',
        [entry(item='ขนมทดสอบไลรา',amount=47),entry(item='เครื่องดื่มทดสอบโนวา',amount=53)])
    return h


def patch(values,*,domain='expense',mutate=None):
    """values: record ordinal -> (field, interpreted value, literal span)."""
    def decide(body):
        context=json.loads(body['input'][0]['content'])
        view=next(v for v in context['pending_records'] if v['domain']==domain)
        args={'draft_ref':view['draft_ref'],'draft_version':view['version'],'records':[
            {'record_ref':view['records'][i]['record_ref'],'changes':[
                {'field':f,'value':v,'source_text':text,'source_ref':context['current_source_ref']} for f,v,text in changes]}
            for i,changes in values.items()]}
        if mutate:mutate(args)
        value=response(name=NAME,args=args)
        value['output'][0]['call_id']='patch_'+digest([args,len(body['input'])])[:20]
        return value
    return decide


@pytest.mark.parametrize('text',['จัดเข้ากลุ่มของกินให้หน่อย','อาหารนั่นแหละ','เอาหมวดที่ใช้กับขนม',
    'ฝั่งของกินได้เลยครับ','ใช้หมวดเกี่ยวกับอาหาร','put it with groceries please'])
def test_semantic_choice_does_not_require_an_enrollment_phrase_or_literal_canonical_label(text):
    h=pending();before=deepcopy(active(h)['entries'])
    result=h.send(text,patch({0:[('category','Food/Grocery',text)]}))
    assert result.status=='clarification',result
    d=active(h)
    assert d['entries'][0]['mapping']['category']=='Food/Grocery'
    assert 'enrollment' not in d['entries'][1]
    for old,current in zip(before,d['entries']):
        for key in ['item','amount','date','note','fact_sources','amount_occurrence','source_quote']:assert old.get(key)==current.get(key)
    assert not h.sheets.entries and d['review'] is None


def test_model_schema_exposes_shared_patch_and_creation_only_legacy_refs():
    h=pending()
    def inspect(body):
        c=json.loads(body['input'][0]['content'])
        assert [r['ordinal'] for r in c['pending_records'][0]['records']]==[1,2]
        tools={t['name']:t for t in body['tools']}
        assert NAME in tools
        assert 'Legacy compatibility' in tools['prepare_item_enrollment']['description']
        assert tools['prepare_expenses']['parameters']['properties']['draft_ref']['enum']==[None]
        assert tools['prepare_table_change']['parameters']['properties']['draft_ref']['enum']==[None]
        return response(text='synthetic read only')
    h.send('ขอดูความสามารถ',inspect)
    assert not h.sheets.entries


def test_pending_schema_does_not_offer_disabled_catalog_enrollment():
    h=pending();h.service.item_enrollment_enabled=False
    def inspect(body):
        views=json.loads(body['input'][0]['content'])['pending_records']
        assert all(not {'category','subcategory','catalog_action','existing_item'}&{f['name'] for f in r['fields']}
            for v in views for r in v['records'])
        return response(text='synthetic capability answer')
    h.send('ทำอะไรกับร่างนี้ได้บ้าง',inspect)
    assert not h.sheets.entries


def test_rename_and_category_together_do_not_require_the_old_name_in_the_reply():
    h=pending();before=deepcopy(active(h));name='ร้านทดสอบออโรรา'
    text=f'ขอเรียกว่า {name} แล้วจัดฝั่งของกินนะ'
    result=h.send(text,patch({0:[('item',name,name),('category','Food/Grocery','ของกิน')]}))
    assert result.status=='clarification',result
    d=active(h)
    assert d['entries'][0]['item']==name and d['entries'][0]['mapping']['category']=='Food/Grocery'
    assert d['entries'][1]==before['entries'][1]
    for key in ['amount','date','note','amount_occurrence']:assert d['entries'][0].get(key)==before['entries'][0].get(key)
    assert d['entries'][0]['fact_sources']['amount']==before['entries'][0]['fact_sources']['amount']
    assert not h.sheets.entries


def test_two_rows_can_be_completed_in_one_patch_and_confirmed_once():
    h=pending()
    result=h.send('ตัวแรกเป็นอาหาร ตัวหลังเป็นของขวัญ',patch({
        0:[('category','Food/Grocery','อาหาร')],1:[('category','Gift','ของขวัญ')]}))
    assert result.status=='preview',result
    assert [e['mapping']['category'] for e in active(h)['entries']]==['Food/Grocery','Gift']
    assert not h.sheets.entries
    event=h.event(result.data['confirm_command']);calls=len(h.port.requests)
    saved=h.service.handle(event)
    assert saved.status=='recorded' and len(h.sheets.entries)==2 and len(h.port.requests)==calls
    assert h.service.handle(deepcopy(event))==saved and len(h.sheets.entries)==2


@pytest.mark.parametrize('field,value,literal',[('amount',75,'75'),('category','Food/Grocery','ของกิน')])
def test_explicit_no_save_correction_can_change_the_draft_without_authorizing_writes(field,value,literal):
    h=pending();before=deepcopy(active(h))
    text='ไม่ต้องบันทึก แก้ยอดอันแรกเป็น 75 บาท' if field=='amount' else 'ไม่ต้องบันทึก แก้หมวดอันแรกเป็นของกิน'
    r=h.send(text,patch({0:[(field,value,literal)]}))
    assert r.status=='clarification',r
    assert active(h)['entries'][1]==before['entries'][1]
    if field=='amount':assert active(h)['entries'][0]['amount']=='75'
    else:assert active(h)['entries'][0]['mapping']['category']=='Food/Grocery'
    assert not h.sheets.entries and active(h)['review'] is None


def test_invalid_sibling_choice_rolls_back_all_prepared_changes():
    h=pending();h.sheets.rows.append(('หมวดทดสอบย่อย','Food/Grocery','Dessert'))
    before=deepcopy(h.state.drafts)
    r=h.send('เปลี่ยนชื่อเป็น ร้านทดสอบใหม่ อาหาร ขนม',patch({
        0:[('item','ร้านทดสอบใหม่','ร้านทดสอบใหม่'),('category','Food/Grocery','อาหาร')],
        1:[('category','Gift','อาหาร'),('subcategory','Dessert','ขนม')]}),response(text='synthetic clarification'))
    assert r.status!='preview' and h.state.drafts==before and not h.sheets.entries


@pytest.mark.parametrize('damage,code',[
    ('version','patch_stale_version'),('record','patch_unknown_record'),('field','patch_unknown_field'),
    ('enum','patch_invalid_value'),('span','patch_owner_evidence'),('source','source_not_in_visible_context'),
    ('literal','patch_literal_value'),('duplicate','patch_duplicate_field')])
def test_invalid_proposals_preserve_facts_and_never_write(damage,code):
    h=pending();before=deepcopy(h.state.drafts)
    def corrupt(a):
        c=a['records'][0]['changes'][0]
        if damage=='version':a['draft_version']+=1
        elif damage=='record':a['records'][0]['record_ref']='foreign_record'
        elif damage=='field':c['field']='invented_column'
        elif damage=='enum':c['value']='Invented Category'
        elif damage=='span':c['source_text']='absent quote'
        elif damage=='source':c['source_ref']='assistant_or_foreign_source'
        elif damage=='literal':c.update(field='item',value='invented name')
        else:a['records'][0]['changes'].append(deepcopy(c))
    result=h.send('อาหาร',patch({0:[('category','Food/Grocery','อาหาร')]},mutate=corrupt),response(text='synthetic clarification'))
    assert result.data['tool_result']['error']==code,result
    assert h.state.drafts==before and not h.sheets.entries


def test_bad_argument_is_returned_with_schema_then_repaired_in_the_same_turn():
    h=pending();before=len(h.port.requests)
    def corrupt(a):a['records'][0]['changes'][0]['field']='invented_column'
    def repaired(body):
        output=json.loads(next(v['output'] for v in body['input'] if v.get('type')=='function_call_output'))
        feedback=output['repair_feedback']
        assert feedback['kind']=='assistant_arguments' and feedback['pending_records']
        assert feedback['allowed_tools']==[NAME,'resolve_temporal']
        assert {t['name'] for t in body['tools']}=={NAME,'resolve_temporal'}
        return patch({0:[('category','Food/Grocery','อาหาร')]})(body)
    r=h.send('อาหาร',patch({0:[('category','Food/Grocery','อาหาร')]},mutate=corrupt),repaired)
    assert r.status=='clarification' and active(h)['entries'][0]['mapping']['category']=='Food/Grocery'
    assert len(h.port.requests)==before+2 and not h.sheets.entries


def test_repeated_bad_plan_stops_before_another_model_request():
    h=pending();before=deepcopy(active(h));calls=len(h.port.requests)
    def corrupt(a):a['records'][0]['changes'][0]['field']='invented_column'
    first=patch({0:[('category','Food/Grocery','อาหาร')]},mutate=corrupt)
    def again(body):
        r=first(body);r['output'][0]['call_id']='a_distinct_call_id';return r
    r=h.send('อาหาร',first,again,response(text='must not run'))
    assert r.data['tool_result']['repair_stopped']=='repeated_failure'
    assert len(h.port.requests)==calls+2 and active(h)==before and not h.sheets.entries


def test_unavailable_repair_preserves_the_original_diagnosis():
    h=pending()
    def corrupt(a):a['records'][0]['changes'][0]['field']='invented_column'
    r=h.send('อาหาร',patch({0:[('category','Food/Grocery','อาหาร')]},mutate=corrupt),TimeoutError('synthetic unavailable'))
    assert r.data['tool_result']['error']=='patch_unknown_field' and 'schema' in r.message
    assert not h.sheets.entries


def test_schema_implied_action_is_omitted_but_the_changed_field_requires_current_evidence():
    h=pending()
    def redundant(args):
        # A retained label is unnecessary as new evidence of catalog creation.
        args['records'][0]['changes'].append({'field':'catalog_action','value':'new',
            'source_text':'retained label is not a new fact','source_ref':'old_unneeded_ref'})
    r=h.send('จัดเข้าของกิน',patch({0:[('category','Food/Grocery','ของกิน')]},mutate=redundant))
    assert r.status=='clarification' and active(h)['entries'][0]['mapping']['category']=='Food/Grocery'
    assert not h.sheets.entries


def test_failed_patch_cannot_claim_a_change_even_if_the_model_says_it_succeeded():
    h=pending();before=deepcopy(active(h))
    def corrupt(a):a['records'][0]['changes'][0]['field']='invented_column'
    r=h.send('ของกิน',patch({0:[('category','Food/Grocery','ของกิน')]},mutate=corrupt),response(text='จัดหมวดให้เรียบร้อยแล้วครับ'))
    assert 'จัดหมวดให้เรียบร้อยแล้ว' not in r.message and 'schema' in r.message
    assert active(h)==before and not h.sheets.entries


def test_failed_patch_keeps_record_focus_for_a_short_next_turn_answer():
    h=pending()
    def corrupt(a):a['records'][0]['changes'][0]['value']='Not a category'
    failed=h.send('ของกิน',patch({0:[('category','Food/Grocery','ของกิน')]},mutate=corrupt),response(text='เลือกหมวดไหนครับ'))
    assert failed.data['tool_result']['pending_focus']['record_ref']==active(h)['entries'][0]['entry_ref']
    def next_turn(body):
        view=json.loads(body['input'][0]['content'])['pending_records'][0]
        assert [r['question_focus'] for r in view['records']]==[True,False]
        return patch({0:[('category','Food/Grocery','Food')]})(body)
    h.send('Food',next_turn)
    assert active(h)['entries'][0]['mapping']['category']=='Food/Grocery' and not h.sheets.entries


def test_known_name_can_be_replaced_with_a_new_literal_name_and_category():
    h=harness();prepare(h,'ร้านทดสอบโคเมต 47 บาท',[entry(item='ร้านทดสอบโคเมต',amount=47)])
    r=h.send('เรียกว่า ร้านทดสอบออโรรา จัดเป็นของกิน',patch({0:[
        ('item','ร้านทดสอบออโรรา','ร้านทดสอบออโรรา'),('category','Food/Grocery','ของกิน')]}))
    assert r.status=='preview',r
    assert active(h)['entries'][0]['item']=='ร้านทดสอบออโรรา' and not h.sheets.entries


def test_new_table_schema_is_patched_without_a_table_specific_function():
    from line_assistant.tables import Table,Column,Registry,MemoryTableStore
    from test_table_workflow import harness as table_harness,prepare as table_prepare
    table=Table(id='plants',label='ต้นไม้ทดสอบ',description='ชื่อและการดูแลต้นไม้',operations=['query','insert'],columns=[
        Column(name='nickname',label='ชื่อเรียก',type='text',required=True),Column(name='watered',label='รดน้ำแล้ว',type='boolean',required=True)])
    store=MemoryTableStore('synthetic-plants');h=table_harness(Registry([table],{'plants':store}))
    partial=h.send('เพิ่มต้นไม้ชื่อ ดาวทดสอบ',table_prepare('plants',nickname=('ดาวทดสอบ','ดาวทดสอบ')))
    assert partial.status=='clarification'
    review=h.send('รดน้ำเรียบร้อยแล้ว',patch({0:[('watered',True,'รดน้ำเรียบร้อยแล้ว')]},domain='table'))
    assert review.status=='preview',review
    assert review.data['table_draft']['values']=={'nickname':'ดาวทดสอบ','watered':True} and store.writes==0
    assert h.send('ยืนยัน').status=='recorded' and store.writes==1


def test_calendar_uses_the_same_patch_shape_and_retains_unchanged_facts():
    from test_calendar_mvp import Harness
    h=Harness();initial=h.prepare('นัดทดสอบดาว พรุ่งนี้',title='ทดสอบดาว',date_text='พรุ่งนี้')
    assert initial.status=='clarification'
    before=deepcopy(h.draft())
    r=h.send('เริ่ม 09:00 จบ 10:00',patch({0:[('time_text','09:00','09:00'),('end_time_text','10:00','10:00')]},domain='calendar'))
    assert r.status=='preview',r
    assert h.draft()['facts']['title']==before['facts']['title'] and h.draft()['facts']['date_text']==before['facts']['date_text']
    assert not h.provider.posts and not h.sheets.entries


@pytest.mark.parametrize('damage',[None,'version','field','duplicate','record'])
def test_unchanged_calendar_patch_keeps_provenance_and_still_checks_target(damage):
    from test_calendar_mvp import Harness,complete
    h=Harness();complete(h);before=deepcopy(h.draft())
    def mutate(args):
        if damage=='version':args['draft_version']+=1
        elif damage=='field':args['records'][0]['changes'][0]['field']='invented_field'
        elif damage=='duplicate':args['records'][0]['changes']*=2
        elif damage=='record':args['records'][0]['record_ref']='foreign_record'
    r=h.send('เหมือนเดิม',patch({0:[('duration_text',before['facts']['duration_text'],'เหมือนเดิม')]},domain='calendar',mutate=mutate),response(text='ขอตรวจร่างครับ'))
    if damage is None:
        assert r.status=='answer' and r.data['tool_result']['status']=='pending_unchanged',r
    else:assert r.data['tool_result']['ok'] is False,r
    assert h.draft()==before and not h.provider.posts


def test_calendar_batch_semantic_target_uses_verified_member_refs_and_fresh_review():
    from test_calendar_mvp import Harness
    from test_calendar_batches_v13 import two,children
    h=Harness();old=two(h,complete=True);before=deepcopy(children(h))
    result=h.send('อันหลังสุดขอเริ่ม 09:30',patch({1:[('time_text','09:30','09:30')]},domain='calendar_batch'))
    assert result.status=='preview',result
    current=children(h)
    assert current[0]['facts']==before[0]['facts']
    assert current[1]['facts']['time_text']=='09:30' and current[1]['facts']['end_time_text']=='17:00'
    assert result.data['confirm_command']!=old.data['confirm_command'] and not h.provider.posts
    assert h.send(old.data['confirm_command']).status=='stale' and not h.provider.posts


def test_generic_table_error_keeps_partial_draft_and_blocks_old_confirmation():
    from test_table_workflow import harness as table_harness,prepare as table_prepare
    h=table_harness();partial=h.send('เพิ่มคุณจำลอง',table_prepare(name=('คุณจำลอง','คุณจำลอง')))
    original=deepcopy(partial.data['table_draft'])
    def corrupt(a):a['records'][0]['changes'][0]['field']='invented_field'
    result=h.send('เบอร์ 0000123',patch({0:[('phone','0000123','0000123')]},domain='table',mutate=corrupt),response(text='ขอตรวจฟิลด์อีกครั้ง'))
    assert result.data['table_draft']==original
    assert h.send('ยืนยัน').status=='stale'
    good=h.send('เบอร์ 0000123',patch({0:[('phone','0000123','0000123')]},domain='table'))
    assert good.status=='preview' and good.data['table_draft']['values']['name']=='คุณจำลอง'
    assert h.app.table_flow.registry.stores['people'].writes==0


@pytest.mark.parametrize('text',['อย่าเพิ่มหมวดอาหาร','สมมติว่าเลือกอาหาร','"อาหาร"'])
def test_non_action_can_be_answered_without_a_tool_or_draft_change(text):
    h=pending();before=deepcopy(active(h))
    h.send(text,response(text='ยังไม่เปลี่ยนร่างครับ'))
    assert active(h)==before and not h.sheets.entries
