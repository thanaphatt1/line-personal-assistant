"""Deterministic explanations derived only from stored server validation results."""
import re

REASONS={
 'owner_no_write':'ข้อความนี้ยังไม่ชัดว่าต้องการเปลี่ยนร่างครับ ข้อมูลเดิมยังอยู่ ระบุรายการและสิ่งที่ต้องการเปลี่ยนได้เลย',
 'ambiguous_unknown_item':'มีหลายรายการที่ยังต้องเลือกชื่อครับ ต้องการเพิ่มชื่อใหม่ให้รายการไหน ระบุชื่อหรือหมายเลขรายการได้เลย',
 'new_item_already_started':'เริ่มเตรียมชื่อใหม่นี้ไว้แล้วครับ กรุณาเลือกหมวดจากร่างที่แสดง',
 'stale_draft_version':'ร่างเปลี่ยนไปแล้วครับ ขอใช้ข้อมูลร่างล่าสุดก่อนทำต่อ',
 'older_draft_update':'คำตอบนี้มาก่อนการแก้ร่างล่าสุดครับ จึงยังไม่เปลี่ยนร่าง',
 'count_amount_mismatch':'จำนวนเที่ยว/ขาที่ระบุไม่ตรงกับจำนวนยอดเงิน จึงยังไม่สร้างรายการบันทึก',
 'count_price_basis_ambiguous':'ระบุหลายเที่ยว/ขาแต่มีเงินเพียงยอดเดียว ยอดนี้เป็นยอดรวม หรือราคาต่อเที่ยว/ขาครับ?',
 'unsupported_expense_count':'จำนวนเที่ยว/ขาที่ระบุยังตรวจสอบไม่ได้ รองรับจำนวนชัดเจน 1–5 รายการต่อร่าง',
 'amount_occurrence_missing_or_reused':'ยังจับคู่ยอดเงินแต่ละรายการกับตำแหน่งตัวเลขต้นฉบับที่ไม่ซ้ำกันไม่ได้',
 'expense_batch_incomplete':'ข้อเสนอของโมเดลยังไม่ครบทุกชื่อและยอดในข้อความ จึงยังไม่ให้ยืนยันหรือบันทึกเฉพาะบางรายการ',
 'ambiguous_date_scope':'วันที่ท้ายข้อความหมายถึงรายการไหนครับ: ทุกรายการ หรือเฉพาะรายการสุดท้าย? ชื่อและยอดที่ตรวจสอบได้ยังอยู่ในร่าง',
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

def codes(output):
 if not isinstance(output,dict):return []
 values=[output.get('error')]
 values += [str(v).split('.')[-1] for key in ('invalid_fields','validation_issues') for v in output.get(key,[])]
 values += [v.get('issue') for v in output.get('date_details',[]) if isinstance(v,dict)]
 values += [v.get('date_issue') for v in output.get('entries',[]) if isinstance(v,dict)]
 return list(dict.fromkeys(v for v in values if isinstance(v,str) and re.fullmatch(r'[a-z_]+',v)
                            and (v in REASONS or output.get('ok') is False or output.get('validation_issues'))))

def message(output):
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
 unknown='ระบบยังจับคู่หลักฐานของรายการได้ไม่ครบ จึงยังระบุสาเหตุที่เจาะจงกว่านี้ไม่ได้'
 reasons=list(dict.fromkeys(REASONS.get(v,unknown) for v in values))
 return 'ยังไม่ได้บันทึก: '+'; '.join(reasons)+rows+'\nข้อความต้นฉบับยังอยู่ ไม่ต้องส่งประโยคเดิมซ้ำ'

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
