"""Recheck pending catalog choices without changing owner facts or consent.

An unrelated catalog edit must not make a sparse pending update impossible.
Revalidation is a pure staging operation. Callers still verify owned versions,
seal the replacement draft and issue a new review before any provider write.
"""
from copy import deepcopy
from .history_context import digest
from .taxonomy import identifier

RESOLUTION_FIELDS={'item_resolution','existing_or_new','category','subcategory'}


def revalidate(draft,tax):
    fresh=deepcopy(draft)
    current=digest([[r.item,r.category,r.subcategory] for r in tax.rows])
    if draft['catalog_hash']==current:return fresh
    missing=[f for f in draft['missing_fields'] if f.rsplit('.',1)[-1] not in RESOLUTION_FIELDS]
    for index,entry in enumerate(fresh['entries']):
        def need(field):missing.append(f'entries[{index}].{field}')
        item=entry.get('item')
        entry.pop('name_candidates',None)
        if not item:continue
        if tax.catalog.match(item).status=='exact':
            entry['mapping']=None
            entry.pop('enrollment',None)
            continue
        selection=deepcopy(entry.get('enrollment'))
        mapping=entry.get('mapping')
        if selection is None and mapping:
            selection={'category_id':identifier('cat',mapping['category']),
                'subcategory_id':identifier('sub',mapping['category']+'\0'+mapping['subcategory'])}
        entry['mapping']=None
        if selection is None:
            # Removed/renamed known names and unresolved names require a choice.
            need('item_resolution')
            continue
        category=selection.get('category_id')
        subcategory=selection.get('subcategory_id')
        if category not in tax.categories:category=None;subcategory=None
        elif subcategory not in tax.pairs[category]:subcategory=None
        selection.update(category_id=category,subcategory_id=subcategory)
        entry['enrollment']=selection
        if category is None:need('category')
        elif subcategory is None:need('subcategory')
        else:entry['mapping']=vars(tax.mapping(item,category,subcategory))
    fresh.update(catalog_hash=current,review=None,missing_fields=list(dict.fromkeys(missing)),
        status='needs_information' if missing else 'review')
    return fresh
