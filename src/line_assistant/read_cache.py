"""Successful read snapshots live for one owner turn, never across turns/writes."""
from contextlib import contextmanager
from contextvars import ContextVar
from copy import deepcopy

ACTIVE=ContextVar('assistant_read_cache',default=None)


@contextmanager
def scope():
    token=ACTIVE.set({})
    try:yield
    finally:ACTIVE.reset(token)


def invalidate():
    cache=ACTIVE.get()
    if cache is not None:cache.clear()


def read(key,load):
    cache=ACTIVE.get()
    if cache is None:return load()
    from .action_trace import emit
    if key in cache:
        emit('read_cache',status='hit')
        return deepcopy(cache[key])
    value=load()  # Failed and incomplete reads are never cached.
    # Eviction changes performance only, never the completeness of a result.
    if len(cache)>=8:cache.pop(next(iter(cache)))
    cache[key]=deepcopy(value)
    emit('read_cache',status='miss')
    return value
