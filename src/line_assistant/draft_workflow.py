"""Typed, read-only pending steps and choices derived from owned draft/catalog.

No owner-text routing, automatic enrollment or write permission is inferred here.
"""
from copy import deepcopy
from .catalog import normalized
from .history_context import draft_index
from .catalog_names import choices as name_choices,focused_choice

def views(h):
    result=draft_index(h.state)
    if not result:return result
    tax=h._taxonomy()
    for view in result:
        from .pending_catalog import revalidate
        d=revalidate(h._draft(view['draft_ref']),tax)
        view.update(entries=[{k:e[k] for k in ('entry_ref','item','amount','date','fact_sources') if k in e} for e in d['entries']],missing_fields=deepcopy(d['missing_fields']),item_resolutions=[],fact_completion=[])
        for e in d['entries']:
            if e.get('previous_price'):
                from .previous_prices import public
                next(v for v in view['entries'] if v['entry_ref']==e['entry_ref'])['previous_price']=public(e['previous_price'])
            missing=[k for k in ['item','amount','date'] if e[k] is None or e[k]=='']
            if missing:view['fact_completion'].append({'entry_ref':e['entry_ref'],'missing':missing,'financial_write_authorized':False})
            if not e['item'] or tax.catalog.match(e['item']).status=='exact':continue
            selected=e.get('enrollment');category=selected.get('category_id') if selected else None
            subcategory=selected.get('subcategory_id') if selected else None
            if category and category not in tax.categories:raise ValueError('category_not_in_taxonomy')
            if subcategory and subcategory not in tax.pairs.get(category,{}):raise ValueError('subcategory_not_in_selected_category')
            names=name_choices(e,tax.catalog)
            names+= [r.item for r in tax.rows if r.item not in names and normalized(e['item']) in normalized(r.item)]
            rows={r.item:r for r in tax.rows}
            choices=[{'item':name,'category':rows[name].category,'subcategory':rows[name].subcategory} for name in names[:5]]
            step='choose_existing_or_new' if selected is None else 'choose_category' if category is None else 'choose_subcategory' if subcategory is None else 'resolved'
            categories=tax.choices(e['item'])['categories']
            view['item_resolutions'].append({'entry_ref':e['entry_ref'],'item':e['item'],'step':step,
                'question_focus':focused_choice(h,d,e) is True,
                'existing_item_choices':choices,'category_choices':categories,
                'selected_category':{'id':category,'label':tax.categories[category]} if category else None,
                'subcategory_choices':[{'id':k,'label':v} for k,v in tax.pairs.get(category,{}).items()][:8],
                'available_operations':['show_choices',*(['begin_new_item','use_existing_item','choose_category'] if selected is None else ['choose_category','choose_subcategory'])],
                'classification_status':'proposal_requires_owner_review' if category else 'unresolved',
                'financial_write_authorized':False})
    return result

def definitions(base,available,date_references=(),*,previous_prices=False):
    # Catalog discovery belongs to preparation. Every turn already has the
    # owned pending facts; separate reads formerly used up the initial turn
    # as well as unnamed/partial draft turns. Free-text answers remain valid.
    result=[t for t in deepcopy(base) if t['name']!='lookup_catalog' and (t['name']!='read_expense_drafts' or available)]
    pending=[r for d in available for r in d.get('item_resolutions',[])]
    # Complete facts/choices are already in the owned context. Read-before-
    # action alternatives add no evidence and formerly exhausted both calls.
    for t in result:
        p=t['parameters']['properties']
        if t['name']=='prepare_expenses':
            from .expense_proposals import definition
            t.clear();t.update(definition(date_references))
            continue
        if t['name']=='prepare_item_enrollment':
            steps=['show_choices']
            if any(r['step']=='choose_existing_or_new' for r in pending):steps+=['begin_new_item','use_existing_item','choose_category']
            if any(r['step'] in {'choose_category','resolved'} for r in pending):steps+=['choose_category']
            if any(r['step'] in {'choose_subcategory','resolved'} for r in pending):steps+=['choose_subcategory']
            p['resolution_step']={'type':'string','enum':list(dict.fromkeys(steps or ['begin_new_item']))}
            t['parameters']['required'].append('resolution_step')
            categories=[c for r in pending for c in r['category_choices']]
            subs=[s for c in categories for s in c['subcategories']]
            p['category']={'type':['string','null'],'enum':[None,*dict.fromkeys(v for c in categories for v in [c['id'],c['label']])]}
            p['subcategory']={'type':['string','null'],'enum':[None,*dict.fromkeys(v for s in subs for v in [s['id'],s['label']])]}
            p['existing_item']={'type':['string','null'],'enum':[None,*dict.fromkeys(c['item'] for r in pending for c in r['existing_item_choices'])]}
            t['description']='Use for a requested draft name/category choice. begin_new_item: category/subcategory MUST be null unless the owner explicitly selected an offered category in this answer. Never infer from the name. null lets the server match that answer. choose_category/subcategory propose offered IDs or labels; use_existing_item selects an offered name. show_choices only reads. Proposals require owner review; never writes.'
        if t['name']=='read_expense_drafts':t['description']='Read pending expense facts/choices and verified conversation summary. Does not alter dates, version, nonce or consent, and never creates save buttons.'
        if t['name']=='control_expense_draft':
            p['operation']['enum']=['cancel']
            t['description']='Cancel explicitly requested pending work. Never writes or deletes a recorded expense. Use read_expense_drafts for inspection.'
    if available:
        result.append({'type':'function','name':'prepare_expense_review','strict':True,
            'description':'Create a fresh owner confirmation preview only when the owner requests review/save preparation. This is not reading history or a draft. No model call commits it.',
            'parameters':{'type':'object','properties':{'draft_ref':{'type':'string'},'draft_version':{'type':'integer'}},'required':['draft_ref','draft_version'],'additionalProperties':False}})
    return result
