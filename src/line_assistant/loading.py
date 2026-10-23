"""Best-effort delayed LINE animation for verified owner requests only."""
from contextlib import contextmanager
from contextvars import ContextVar,copy_context
from threading import Timer

OWNER=ContextVar('verified_loading_owner',default=None)
PATH='/v2/bot/chat/loading/start'


@contextmanager
def indicator(assistant,event,dispatcher,*,delay=.8):
    source=event.get('source',{})
    api=getattr(dispatcher,'api',None)
    enabled=(getattr(assistant,'raw_history_enabled',False) and api is not None
        and callable(getattr(api,'loading',None)) and isinstance(source,dict)
        and source.get('type')=='user' and source.get('userId')==assistant.settings.owner_id
        and event.get('type') in {'message','postback'} and event.get('mode','active')=='active'
        and isinstance(event.get('deliveryContext',{}),dict) and not event.get('deliveryContext',{}).get('isRedelivery')
        and isinstance(event.get('webhookEventId'),str)
        and event.get('webhookEventId') not in assistant.state.receipts)
    if not enabled:yield;return
    context=copy_context()
    def show():
        from .action_trace import emit
        token=OWNER.set(assistant.settings.owner_id)
        try:
            from .pilot_http import rpc_budget
            with rpc_budget(1,seconds=1):api.loading(assistant.settings.owner_id)
            emit('loading_indicator',status='shown')
        except Exception:emit('loading_indicator',status='unavailable')
        finally:OWNER.reset(token)
    timer=Timer(delay,lambda:context.run(show));timer.daemon=True;timer.start()
    try:yield
    finally:
        timer.cancel()
        # Finish any already-started one-second request before sending the reply,
        # so a delayed animation cannot arrive after the answer.
        timer.join(timeout=1.2)
