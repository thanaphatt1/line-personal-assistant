"""Unknown expense wording may get existing-name suggestions; the owner still chooses and nothing is written."""
from copy import deepcopy
import json,socket
import pytest
from line_assistant import name_matching as names
from line_assistant.transport import LiveDisabled
from test_catalog_names_v09 import harness,entry
from test_history_first import response
from owner_features_hosted import OwnerHosted,turn


@pytest.fixture(autouse=True)
def offline(monkeypatch):
    def deny(*a,**k):raise AssertionError('Synthetic providers only')
    monkeypatch.setattr(socket.socket,'connect',deny);monkeypatch.setattr(socket.socket,'connect_ex',deny)


def matched(*rows,usage=60):
    text=json.dumps({'candidates':[{'name':n,'confidence':c,'reason':'synthetic transliteration'} for n,c in rows]},ensure_ascii=False)
    return {'model':'gpt-6-luna','status':'completed','usage':{'input_tokens':400,'output_tokens':usage},
        'output':[{'type':'message','status':'completed','content':[{'type':'output_text','text':text}]}]}


def proposal(item='เซเว่น',amount=121):
    def call(body):
        c=json.loads(body['input'][0]['content'])
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'only_if_absent':False,
            'entries':[{**entry(item=item,amount=amount),'source_refs':[c['current_source_ref']]}]})
    return call


def enabled(*catalog):
    h=harness(catalog or ('seven','grab bike'));h.service.name_matching_enabled=True;return h


def matcher_bodies(h):return [b for b in h.port.requests if b.get('instructions')==names.INSTRUCTIONS]


def test_body_only_enumerates_real_names_and_parser_rejects_invented_or_extra_candidates():
    body=names.body_for('เซเว่น',['seven','grab bike'])
    assert names.validate_body(body) and body['tools']==[] and body['reasoning']=={'effort':'none'}
    item=body['text']['format']['schema']['properties']['candidates']['items']['properties']['name']
    assert item=={'type':'string','enum':['seven','grab bike']}
    assert [r['name'] for r in names.parse(matched(('seven','high')),body)]==['seven']
    tampered=deepcopy(body);tampered['input'][0]['content']=tampered['input'][0]['content'].replace('seven','sevenx')
    assert not names.validate_body(tampered)
    for bad in [matched(('7-Eleven','high')),matched(('seven','high'),('seven','low')),
                matched(('seven','high'),('grab bike','low'),('seven','medium'),('grab bike','high'))]:
        with pytest.raises(ValueError):names.parse(bad,body)
    with pytest.raises(OverflowError):names.parse(matched(('seven','high'),usage=10**6),body)


def test_transliterated_wording_asks_about_the_suggested_existing_name_without_any_write():
    h=enabled()
    r=h.send('วันนี้มีเซเว่น 121',proposal(),matched(('seven','high')),response(text='synthetic fallback'))
    assert r.status=='clarification' and 'เซเว่น หมายถึง seven' in r.message and 'หรือเป็นรายการใหม่' in r.message
    assert len(matcher_bodies(h))==1 and not h.sheets.entries and not h.state.actions
    payload=json.loads(matcher_bodies(h)[0]['input'][0]['content'])
    assert payload['owner_wording']=='เซเว่น' and 'seven' in payload['catalog']


@pytest.mark.parametrize('answer',[matched(('seven','low')),matched(),RuntimeError('synthetic outage'),matched(('seven','high'),usage=10**6)])
def test_unhelpful_or_failed_suggestions_keep_the_existing_new_name_question(answer):
    h=enabled()
    r=h.send('วันนี้มีเซเว่น 121',proposal(),answer,response(text='synthetic fallback'))
    assert r.status=='clarification' and 'ยังไม่พบชื่อ เซเว่น' in r.message and 'หมายถึง seven' not in r.message
    assert not h.sheets.entries and len(matcher_bodies(h))==1


def test_known_name_or_disabled_flag_makes_no_matching_request():
    h=enabled();h.send('grab bike 37',proposal('grab bike',37),response(text='synthetic fallback'))
    assert not matcher_bodies(h)
    h=harness(('seven',));h.send('วันนี้มีเซเว่น 121',proposal(),response(text='synthetic fallback'))
    assert not matcher_bodies(h)


def test_duplicate_delivery_replays_the_receipt_without_a_second_matching_request():
    h=enabled();event=h.event('วันนี้มีเซเว่น 121')
    h.port.values=[proposal(),matched(('seven','high')),response(text='synthetic fallback')]
    first=h.service.handle(deepcopy(event));calls=len(h.port.requests)
    again=h.service.handle(deepcopy(event))
    assert again.message==first.message and len(h.port.requests)==calls and len(matcher_bodies(h))==1


def hosted_proposal(item,amount=88):
    def call(body):
        c=json.loads(body['input'][0]['content'])
        return response(name='prepare_expenses',args={'draft_ref':None,'draft_version':None,'only_if_absent':False,'entries':[{'entry_ref':None,'item':item,'item_text':None,
            'amount':amount,'date_text':None,'date_ref':None,'note':None,'source_refs':[c['current_source_ref']],'field_sources':None,'previous_price_ref':None}]})
    return call


def test_signed_production_boundary_admits_only_the_reserved_matching_request():
    api=OwnerHosted();api.owner_features['PILOT_NAME_MATCHING_ENABLED']=True
    r,_=turn(api,'สปอติฟาย 88 บาท',hosted_proposal('สปอติฟาย'),matched(('Spotify','high')))
    assert r.status=='clarification' and 'สปอติฟาย หมายถึง Spotify' in r.message and api.expense_writes==0
    sent=[b for b in api.model_bodies if b.get('instructions')==names.INSTRUCTIONS]
    assert len(sent)==1 and names.validate_body(sent[0])
    app=api.app()
    try:
        with pytest.raises(LiveDisabled):app.state.pilot_http.allowed('model','POST','/v1/responses',deepcopy(sent[0]),None)
        with pytest.raises(LiveDisabled):app.state.pilot_http.allowed('model','POST','/v1/responses',names.body_for('อื่น',['Spotify']),None)
    finally:app.state.pilot_http.close()


def test_typed_save_label_confirms_the_reviewed_choice_and_a_false_success_claim_never_crashes():
    api=OwnerHosted();api.owner_features['PILOT_NAME_MATCHING_ENABLED']=True
    turn(api,'สปอติฟาย 88 บาท',hosted_proposal('สปอติฟาย'),matched(('Spotify','high')))
    from test_entry_checks import existing
    review,_=turn(api,'ใช่',existing('Spotify'));assert review.status=='preview' and api.expense_writes==0
    claimed,_=turn(api,'ตกลงตามนั้น',response(text='บันทึกรายการ Spotify 88 บาทเรียบร้อยแล้วครับ'))
    assert claimed.status=='clarification' and 'ยังไม่ได้บันทึก' in claimed.message and api.expense_writes==0
    again,_=turn(api,'ขอดูอีกครั้ง',response(name='prepare_expense_review',args={'draft_ref':review.data['tool_result']['draft_ref'],
        'draft_version':review.data['tool_result']['draft_version']}))
    saved,_=turn(api,'บันทึก')
    assert again.status=='preview' and saved.status=='recorded' and api.expense_writes>=1
