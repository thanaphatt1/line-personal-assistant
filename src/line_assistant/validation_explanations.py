"""Grounded failure feedback for the reply model and its offline fallback.

Diagnosis comes from server validation, never an LLM guess. Owned draft facts
and offered choices can turn an implementation failure into a useful question
without selecting a category, changing a draft or authorizing a write.
"""
from copy import deepcopy
import json,re

REASONS={
 'owner_no_write':'ต้องการเปลี่ยนรายการไหนและเปลี่ยนอะไรครับ?',
 'ambiguous_unknown_item':'มีหลายรายการที่ยังต้องเลือกชื่อครับ ต้องการเพิ่มชื่อใหม่ให้รายการไหน ระบุชื่อหรือหมายเลขรายการได้เลย',
 'new_item_already_started':'เริ่มเตรียมชื่อใหม่นี้ไว้แล้วครับ กรุณาเลือกหมวดจากรายการที่แสดง',
 'stale_draft_version':'รายการเปลี่ยนไปแล้วครับ ขอใช้ข้อมูลล่าสุดก่อนทำต่อ',
 'older_draft_update':'คำตอบนี้มาก่อนการแก้ไขรายการล่าสุดครับ จึงยังไม่เปลี่ยนข้อมูล',
 'new_item_requires_category_clarification':'ผมส่งหมวดหรือหมวดย่อยที่ยังยืนยันจากคำตอบของคุณไม่ได้ จึงยังไม่ใช้หมวดนั้นครับ',
 'category_step_not_ready':'ขั้นตอนเลือกหมวดยังไม่พร้อม จึงยังไม่เปลี่ยนหมวดครับ',
 'subcategory_step_not_ready':'ยังเลือกหมวดย่อยไม่ได้ ต้องตรวจหมวดหลักก่อนครับ',
 'category_not_offered':'หมวดที่ผมส่งไปไม่ตรงกับตัวเลือกที่ระบบให้ใช้ครับ',
 'subcategory_not_in_selected_category':'หมวดย่อยที่ส่งไปไม่อยู่ในหมวดหลักที่เลือกครับ',
 'invalid_tool_arguments':'ผมส่งข้อมูลให้เครื่องมือผิดรูปแบบ จึงยังทำขั้นตอนนี้ต่อไม่ได้ครับ',
 'source_not_in_visible_context':'ผมอ้างอิงข้อความต้นทางไม่ตรงกับประวัติที่ระบบให้ใช้ จึงยังไม่ใช้ข้อเสนอนี้ครับ',
 'field_source_unavailable':'ผมอ้างแหล่งข้อมูลของบางฟิลด์ไม่ตรงกับข้อความที่ใช้ตรวจสอบได้ จึงยังไม่ใช้ข้อเสนอนี้ครับ',
 'field_source_not_owner_fact':'ผมจับคู่ชื่อ ยอด หรือวันที่กับข้อความต้นทางที่อ้างไว้ไม่ได้ จึงยังไม่ใช้ข้อเสนอนี้ครับ',
 'category_fields_belong_to_item_resolution':'ผมส่งหมวดผ่านขั้นตอนแก้ข้อมูลรายจ่าย ซึ่งต้องใช้ขั้นตอนเลือกหมวดแยกกัน จึงยังไม่เปลี่ยนข้อมูลครับ',
 'count_amount_mismatch':'จำนวนเที่ยว/ขาที่ระบุไม่ตรงกับจำนวนยอดเงิน จึงยังไม่สร้างรายการบันทึก',
 'count_price_basis_ambiguous':'ระบุหลายเที่ยว/ขาแต่มีเงินเพียงยอดเดียว ยอดนี้เป็นยอดรวม หรือราคาต่อเที่ยว/ขาครับ?',
 'unsupported_expense_count':'จำนวนเที่ยว/ขาที่ระบุยังตรวจสอบไม่ได้ รองรับจำนวนชัดเจน 1–5 รายการต่อชุด',
 'amount_occurrence_missing_or_reused':'ยังจับคู่ยอดเงินแต่ละรายการกับตำแหน่งตัวเลขต้นฉบับที่ไม่ซ้ำกันไม่ได้',
 'expense_batch_incomplete':'ข้อเสนอของโมเดลยังไม่ครบทุกชื่อและยอดในข้อความ จึงยังไม่ให้ยืนยันหรือบันทึกเฉพาะบางรายการ',
 'reused_expense_fields_required':'ผมยังใส่ชื่อ ยอด หรือแหล่งอ้างอิงของรายการเดิมไม่ครบในข้อเสนอรายการใหม่ครับ',
 'ambiguous_date_scope':'วันที่ท้ายข้อความหมายถึงรายการไหนครับ: ทุกรายการ หรือเฉพาะรายการสุดท้าย?',
 'amount_source':'ยังยืนยันยอดเงินของรายการจากข้อความต้นฉบับไม่ได้',
 'item_source':'ยังยืนยันชื่อเต็มของรายการจากข้อความต้นฉบับไม่ได้',
 'full_item_name_required':'ชื่อที่โมเดลเสนอไม่ครอบคลุมชื่อเต็มในข้อความต้นฉบับ',
 'note_source':'หมายเหตุที่โมเดลเสนอไม่มีช่องหมายเหตุที่เจ้าของระบุในข้อความต้นฉบับ',
 'date_text_source':'ข้อความวันที่ที่โมเดลเสนอไม่ตรงกับวันที่ของรายการในต้นฉบับ',
 'date_source':'วันที่ที่โมเดลเสนอไม่ตรงกับวันที่ที่ระบบตรวจสอบจากต้นฉบับ',
 'date_expression_unresolved':'ยังแปลข้อความวันที่ที่ระบุให้เป็นวันที่ของรายการไม่ได้',
 'ambiguous_date_source':'พบวันที่หลายค่าที่ขัดกันในรายการเดียว กรุณาเลือกวันที่ของรายการนั้น',
 'invalid_calendar_date':'วันที่ที่ระบุไม่มีอยู่จริงตามปฏิทิน',
 'future_expense_date':'วันที่ที่ตรวจสอบได้เป็นอนาคต จึงยังไม่อนุญาตให้บันทึกรายจ่าย',
 'missing_day':'ระบุเดือนหรือปีแล้ว แต่ยังไม่มีวันของรายการ',
 'missing_month_for_future_day':'วันที่ระบุจะเป็นอนาคตถ้าใช้เดือนนี้ กรุณาระบุเดือน',
 'tool_unavailable':'ขั้นตอนตรวจสอบทำงานไม่สำเร็จ จึงยังไม่ได้บันทึก และยังระบุสาเหตุที่เจาะจงกว่านี้ไม่ได้',
}

ENROLLMENT_ERRORS={'new_item_requires_category_clarification','category_step_not_ready',
 'subcategory_step_not_ready','category_not_offered','subcategory_not_in_selected_category','new_item_already_started'}
MODEL_ERRORS=ENROLLMENT_ERRORS|{'invalid_tool_arguments','amount_occurrence_missing_or_reused',
 'expense_batch_incomplete','reused_expense_fields_required','amount_source','item_source','full_item_name_required','note_source',
 'date_text_source','date_source','source_not_in_visible_context','field_source_unavailable',
 'field_source_not_owner_fact','category_fields_belong_to_item_resolution'}


def enrich(h,call,output):
 """Attach bounded, server-owned context to failed expense preparation only."""
 if not isinstance(output,dict) or output.get('ok') is not False:return output
 if call.get('name') not in {'prepare_expenses','prepare_item_enrollment','prepare_expense_review','read_expense_drafts','control_expense_draft'}:return output
 values=codes(output)
 if not values:return output
 kind=('model_arguments' if set(values)<=MODEL_ERRORS else 'unavailable' if values==['tool_unavailable'] else
       'validation' if all(v in REASONS for v in values) else 'unclassified')
 facts={'status':'ยังไม่ได้บันทึกครับ','reason':'; '.join(dict.fromkeys(REASONS.get(v,
     'ระบบทำขั้นตอนนี้ไม่สำเร็จ แต่ผลตรวจยังไม่บอกสาเหตุ จึงยังบอกไม่ได้ว่าคุณต้องแก้ข้อมูลส่วนไหนครับ') for v in values))}
 feedback={'version':1,'tool':call['name'],'codes':values,'kind':kind,
           'invalid_fields':deepcopy(output.get('invalid_fields',[])),
           'required_fields':deepcopy(output.get('missing_fields',[])),
           'next_action':'investigate' if kind=='unclassified' else 'none','facts':facts}
 result={**output,'validation_feedback':feedback}
 from .tool_errors import recovery
 result['recovery']=(output.get('recovery') if output.get('recovery',{}).get('kind')!='unavailable' else None) or recovery(kind='assistant_arguments' if kind=='model_arguments' else 'owner_input' if kind=='validation' else 'unavailable',
     fields=output.get('invalid_fields',[]),required=output.get('missing_fields',[]),tools=[call['name'],'patch_pending_records','resolve_temporal'])
 if call['name']=='prepare_expenses':
  try:creating=json.loads(call['arguments']).get('draft_ref') is None
  except (ValueError,TypeError,AttributeError):creating=False
  if creating:
   # Repair a new-record proposal in that operation. An unrelated pending
   # row cannot become the target merely to get around a source/name error.
   result['recovery']={**result['recovery'],'allowed_tools':['prepare_expenses','resolve_temporal']}
 if values[0] not in ENROLLMENT_ERRORS and all(v in REASONS for v in values):
  # Existing count/date/source diagnostics contain checked rows and questions.
  # Keep those complete; the new metadata must not erase their evidence.
  feedback['facts']={'status':message(output)}
 if values[0] not in ENROLLMENT_ERRORS:return result
 try:
  args=json.loads(call['arguments'])
  d=h._draft(args['draft_ref'])
  if args.get('draft_version')!=d['version'] or getattr(h,'_request_views',{}).get(d['draft_ref'])!=h._view(d):return result
  from .catalog_names import enrollment_target,focused_choice
  tax=h._taxonomy()
  unknown=[e for e in d['entries'] if e.get('item') and tax.catalog.match(e['item']).status!='exact']
  targets=enrollment_target(h,d,unknown) if len(unknown)>1 else unknown
  if len(targets)!=1 and args.get('entry_ref'):
   targets=[e for e in unknown if e['entry_ref']==args['entry_ref'] and focused_choice(h,d,e) is True]
  if len(targets)!=1 or args.get('entry_ref') not in {None,targets[0]['entry_ref']}:return result
  e=targets[0];category=(e.get('enrollment') or {}).get('category_id')
  field='subcategory' if category else 'category'
  choices=([{'id':k,'label':v} for k,v in list(tax.pairs[category].items())[:8]] if category else
           [{'id':c['id'],'label':c['label']} for c in tax.choices(e['item'])['categories']])
  if not choices:return result
  index=d['entries'].index(e)
  feedback.update(required_fields=[f'entries[{index}].{field}'],next_action='ask_'+field,
                  target={'entry_ref':e['entry_ref'],'item':e['item'],'field':field,'choices':choices})
  feedback['known_entries']=[{k:deepcopy(v.get(k)) for k in ('entry_ref','item','amount','date')} for v in d['entries']]
  label='หมวดย่อย' if category else 'หมวด'
  facts['ask']=f"{e['item']} ควรอยู่{label}ไหนครับ: "+' / '.join(c['label'] for c in choices)
  result.update(draft_ref=d['draft_ref'],draft_version=d['version'],catalog_entry_ref=e['entry_ref'],
                item_enrollment={'entry_ref':e['entry_ref'],'item':e['item'],'field':field,'choices':choices})
 except Exception:pass  # Optional diagnosis must not unwind a tool outcome.
 return result

def codes(output):
 if not isinstance(output,dict):return []
 values=[output.get('error')]
 if output.get('error')!='invalid_tool_arguments':
  values += [str(v).split('.')[-1] for key in ('invalid_fields','validation_issues') for v in output.get(key,[])]
 values += [v.get('issue') for v in output.get('date_details',[]) if isinstance(v,dict)]
 values += [v.get('date_issue') for v in output.get('entries',[]) if isinstance(v,dict)]
 result=list(dict.fromkeys(v for v in values if isinstance(v,str) and re.fullmatch(r'[a-z_]+',v)
                            and (v in REASONS or output.get('ok') is False or output.get('validation_issues'))))
 # The envelope is not another unknown cause when field diagnostics exist.
 # Unknown details still remain unknown; genuine owner-input errors stay so.
 return result[1:] if result and result[0]=='invalid_fields' and len(result)>1 else result

def message(output):
 if isinstance(output,dict) and output.get('validation_feedback'):
  return '\n'.join(output['validation_feedback']['facts'].values())
 values=codes(output)
 if not values:return None
 rows=''
 if 'ambiguous_date_scope' in values or output.get('validation_issues'):
  rows='\n'+'\n'.join(f"{e.get('item') or '?'} {e.get('amount') or '?'} บาท วันที่ {e.get('date') or '(รอเลือกขอบเขต)'}" for e in output.get('entries',[]))
 if output.get('validation_issues'):
  labels={'item':'ชื่อรายการ','amount':'จำนวนเงิน','date':'วันที่','day':'วัน','month':'เดือน'}
  missing=list(dict.fromkeys(labels.get(str(f).split('.')[-1],'ข้อมูลรายการ') for f in output.get('missing_fields',[])))
  rows+='\nขอเฉพาะข้อมูลที่ยังตรวจสอบไม่ได้: '+', '.join(missing)
 dates=list(dict.fromkeys(v['normalized_date'] for v in output.get('date_details',[]) if v.get('normalized_date')))
 if dates:rows+='\nวันที่ที่ตรวจสอบได้: '+', '.join(dates)
 unknown='ระบบทำขั้นตอนนี้ไม่สำเร็จ แต่ผลตรวจยังไม่บอกสาเหตุ จึงยังบอกไม่ได้ว่าคุณต้องแก้ข้อมูลส่วนไหนครับ'
 reasons=list(dict.fromkeys(REASONS.get(v,unknown) for v in values))
 return 'ยังไม่ได้บันทึก: '+'; '.join(reasons)+rows

def asks_why(text):
 clean=text.strip().lower()
 return bool(re.fullmatch(r'(?:ทำไม|เพราะอะไร|ติดอะไร|ผิดตรงไหน)(?:ครับ|ค่ะ|คะ)?[ ?？]*',clean)
  or re.search(r'(?:ทำไม|เพราะอะไร|สาเหตุ|ผิดตรงไหน|ติดอะไร|อธิบาย).{0,40}(?:บันทึก|รายการ|ไม่ผ่าน|ไม่ได้|ข้อผิดพลาด|error)',clean)
  or re.search(r'(?:เมื่อกี้|ข้อผิดพลาด|error).{0,25}(?:ทำไม|เพราะ|อะไร|อธิบาย|ผิดตรงไหน)',clean)
  or re.search(r'(?:ทำไม|เพราะ).{0,16}(?:ขาด|ไม่มี|ไม่ระบุ|ต้องระบุ)วันที่',clean))

def latest_scope_question(h):
 """Keep the last delivered scope question across non-mutating bridge replies.

 A newer question for another draft wins. The caller still compares the exact
 owned draft/version; stale, expired and undelivered receipts never bind it.
 """
 from .semantic_tools import message_hash,rendered_message
 for row in reversed(h.state.history):
  if row['role']!='assistant':continue
  receipt=h.state.receipts.get(row['event'])
  if not receipt or receipt.expires_at<=h.clock():continue
  if getattr(h,'production',False):
   reply=h.state.replies.get(row['event'])
   if not reply or reply.status!='sent' or reply.content_hash!=message_hash(rendered_message(receipt.result)):continue
  data=receipt.result.data;validation=data.get('validation_receipt',{})
  if 'ambiguous_date_scope' in validation.get('codes',[]) and validation.get('draft_ref'):return validation
  output=data.get('tool_result',{})
  if output.get('draft_ref'):return {}
 return {}
