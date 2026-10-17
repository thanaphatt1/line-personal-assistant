"""Server-owned catalog/category IDs. Labels are data, never authorization."""
from dataclasses import dataclass
from difflib import SequenceMatcher
import hashlib
import re
from .catalog import ItemCatalog, normalized

def identifier(kind, value):
    return kind+'_'+hashlib.sha256(normalized(value).encode()).hexdigest()[:16]

@dataclass(frozen=True)
class CatalogRow:
    item: str
    category: str
    subcategory: str

class Taxonomy:
    def __init__(self, rows):
        self.rows=tuple(CatalogRow(*r) for r in rows)
        for row in self.rows:
            if any(not isinstance(v,str) or len(v)>200 for v in (row.item,row.category,row.subcategory)) or not row.item.strip():
                raise ValueError('Invalid bounded catalog row')
        self.catalog=ItemCatalog([r.item for r in self.rows],{})
        self.items={identifier('item',r.item):r for r in self.rows}
        self.categories={identifier('cat',r.category):r.category for r in self.rows if r.category.strip()}
        self.pairs={}
        for row in self.rows:
            if row.category.strip():
                self.pairs.setdefault(identifier('cat',row.category),{})[identifier('sub',row.category+'\0'+row.subcategory)]=row.subcategory

    def category_matches(self, value):
        key=normalized(value)
        exact=[k for k,v in self.categories.items() if normalized(v)==key]
        if exact:return exact
        # Component aliases are transparent existing-label matches, never new taxonomy.
        aliases={key,'food'} if key=='อาหาร' else {key}
        return [k for k,v in self.categories.items() if aliases.intersection(normalized(s) for s in re.split(r'[/&]',v)) and key]

    def choices(self, text, pending_items=()):
        probes=[text,*pending_items]
        ranked=[]
        for key,row in self.items.items():
            name=normalized(row.item)
            score=max((1 if name in normalized(p) else SequenceMatcher(None,normalized(p),name).ratio() for p in probes),default=0)
            ranked.append((score,key,row.item))
        items=[{'id':k,'label':label} for score,k,label in sorted(ranked,reverse=True)[:5] if score>=.35]
        related=[k for k,v in self.categories.items() if any(normalized(s) in normalized(text) for s in re.split(r'[/&]',v) if s.strip())]
        ids=list(dict.fromkeys(related+list(self.categories)))[:8]
        categories=[{'id':k,'label':self.categories[k],'subcategories':[{'id':s,'label':v} for s,v in list(self.pairs[k].items())[:8]]} for k in ids]
        return {'items':items,'categories':categories}

    def mapping(self, item, category_id, subcategory_id):
        if category_id not in self.categories or subcategory_id not in self.pairs[category_id]:
            raise ValueError('Select an existing category/subcategory pair')
        return CatalogRow(item,self.categories[category_id],self.pairs[category_id][subcategory_id])
