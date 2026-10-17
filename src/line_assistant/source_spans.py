"""Catalog-equivalent literal matches at immutable owner-text coordinates."""
from dataclasses import dataclass
from functools import lru_cache
import unicodedata
from .catalog import normalized,spacing_key

@dataclass(frozen=True)
class Span:
    text:str
    a:int
    b:int
    def start(self):return self.a
    def end(self):return self.b
    def span(self):return self.a,self.b
    def group(self):return self.text[self.a:self.b]

@lru_cache(maxsize=128)
def _index(text):
    # NFC can compose earlier characters (including Hangul Jamo), and casefold
    # can expand one character. Re-map the changed suffix of each prefix, then
    # verify whole original slices before accepting any match. No source text,
    # source identifier, digest or persisted occurrence offset is rewritten.
    previous='';positions=[]
    for i in range(len(text)):
        current=unicodedata.normalize('NFC',text[:i+1]).casefold()
        if current.startswith(previous):common=len(previous)
        else:
            common=0
            while common<min(len(previous),len(current)) and previous[common]==current[common]:common+=1
        first=min([i]+[a for a,_ in positions[common:]])
        positions=positions[:common]+[(first,i+1)]*(len(current)-common)
        previous=current
    return previous,tuple(positions)

def _latin(char):return char.isalpha() and 'LATIN' in unicodedata.name(char,'')
def _edge(text,left):
    chars=text if left else reversed(text)
    return next((c for c in chars if not unicodedata.combining(c)),'')
def _boundary(text,name,a,b):
    first,last=_edge(name,True),_edge(name,False)
    if a and (_latin(first) or first.isdigit()) and (_latin(text[a-1]) or text[a-1].isdigit() or unicodedata.category(text[a-1]).startswith('M')):return False
    if b<len(text) and (_latin(last) or last.isdigit()):
        following=text[b]
        if _latin(following) or unicodedata.category(following).startswith('M') or last.isdigit() and following.isdigit():return False
    return True

def finditer(text,name,*,whitespace=False):
    if not isinstance(name,str) or not normalized(name):return []
    key=spacing_key if whitespace else normalized
    needle=key(name);folded,positions=_index(text);result=[];cursor=0
    if whitespace:
        pairs=[(c,p) for c,p in zip(folded,positions) if not c.isspace() or c in '\r\n']
        folded=''.join(c for c,_ in pairs);positions=tuple(p for _,p in pairs)
    if not needle:return []
    while True:
        start=folded.find(needle,cursor)
        if start<0:return result
        stop=start+len(needle);cursor=start+1
        a=min(v[0] for v in positions[start:stop]);b=max(v[1] for v in positions[start:stop])
        if (key(text[a:b])==needle and _boundary(text,needle,a,b)
            and (not result or a>=result[-1].b)):
            result.append(Span(text,a,b))

def contains(text,name):return bool(finditer(text,name))

def map_outside(text,name,transform,*,matches=None):
    parts=[];start=0
    for match in finditer(text,name) if matches is None else matches:
        parts.extend([transform(text[start:match.a]),text[match.a:match.b]]);start=match.b
    parts.append(transform(text[start:]));return ''.join(parts)
