"""Opt-in lossless bounded snapshots, preserving every replay/action proof."""
import base64
import json
import lzma
import zlib
from .transport import OutcomeUnknown

MAX_EXPANDED_BYTES = 8_000_000

def encode(snapshot, limit, *, compact=False):
    raw = json.dumps(snapshot, ensure_ascii=False, separators=(',', ':')).encode()
    if compact and len(raw) > limit // 2:
        if len(raw) > MAX_EXPANDED_BYTES: raise OutcomeUnknown('Expanded snapshot capacity exceeded')
        # A 4MiB dictionary at preset3 deduplicates distant immutable proofs;
        # compression is bounded by the expanded cap and uses stdlib only.
        raw = json.dumps({'encoding':'lzma-base64-v1','bytes':len(raw),
                          'data':base64.b64encode(lzma.compress(raw, preset=3)).decode('ascii')}, separators=(',', ':')).encode()
    if len(raw) > limit: raise OutcomeUnknown('State snapshot capacity exceeded')
    return raw.decode('utf8')

def decode(value, limit):
    try:
        if not isinstance(value,str) or len(value.encode()) > limit: raise ValueError()
        parsed = json.loads(value)
        if isinstance(parsed,dict) and 'encoding' in parsed:
            if (set(parsed) != {'encoding','bytes','data'} or parsed['encoding'] not in {'zlib-base64-v1','lzma-base64-v1'}
                    or type(parsed['bytes']) is not int or not 0 < parsed['bytes'] <= MAX_EXPANDED_BYTES): raise ValueError()
            packed = base64.b64decode(parsed['data'], validate=True)
            if parsed['encoding'] == 'zlib-base64-v1':
                inflater = zlib.decompressobj()
                expanded = inflater.decompress(packed, parsed['bytes'] + 1)
                if inflater.unconsumed_tail: raise ValueError()
            else:
                inflater = lzma.LZMADecompressor(format=lzma.FORMAT_XZ, memlimit=64*1024*1024)
                expanded = inflater.decompress(packed, max_length=parsed['bytes'] + 1)
            if (len(expanded) != parsed['bytes'] or not inflater.eof or inflater.unused_data): raise ValueError()
            parsed = json.loads(expanded)
        if not isinstance(parsed,dict): raise ValueError()
        return parsed
    except (ValueError, TypeError, zlib.error, lzma.LZMAError, UnicodeError):
        raise OutcomeUnknown('Snapshot decoding failed within bounded capacity') from None
