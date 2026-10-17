"""One fingerprint contract for text and signed LINE postbacks."""
import hashlib,json

def fingerprint(event):
    value={'user':event.get('source',{}).get('userId'),'timestamp':event.get('timestamp')}
    if event.get('type')=='postback':
        value.update(postback=event.get('postback',{}).get('data'))
    else:
        value.update(text=event.get('message',{}).get('text'),message_id=event.get('message',{}).get('id'))
    return hashlib.sha256(json.dumps(value,sort_keys=True,ensure_ascii=False).encode()).hexdigest()
