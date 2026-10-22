"""Positive compaction fixtures adapt to the current fixed tool header."""
from line_assistant import raw_history as raw


def require_compaction(monkeypatch,host,*,excess=1000):
    original=raw.request
    capacity=None
    def request(*args,**kwargs):
        nonlocal capacity
        if capacity is None:
            body,_,_=original(*args,**{**kwargs,'context_tokens':raw.MODEL_CONTEXT_TOKENS})
            capacity=raw.tokens_bound(body)+body['max_output_tokens']-excess
            host.raw_context_tokens=capacity
        return original(*args,**{**kwargs,'context_tokens':capacity})
    monkeypatch.setattr(raw,'request',request)
