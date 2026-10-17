"""Server-built LINE Flex messages with short, version-bound postback actions."""
import hashlib
import json

def version(pending):
    raw = json.dumps(pending.payload, sort_keys=True, ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode()).hexdigest()[:16]

def data(pending, operation, value=''):
    return '|'.join(('v1', pending.token, version(pending), operation, value))

def utf16_limit(text, units):
    return text.encode('utf-16-le')[:units * 2].decode('utf-16-le', errors='ignore')

def text_chunks(text, units=2000):
    chunks = []
    while text:
        chunk = utf16_limit(text, units)
        chunks.append(chunk)
        text = text[len(chunk):]
    return chunks

def message(text, pending, choices):
    # Basic Flex components work on LINE mobile and desktop; no quick-reply dependency.
    buttons = []
    for label, operation, value in choices[:10]:
        buttons.append({'type': 'button', 'style': 'secondary', 'height': 'sm',
                        'action': {'type': 'postback', 'label': utf16_limit(label, 20),
                                   'displayText': utf16_limit(label, 100), 'data': data(pending, operation, value)}})
    return render_actions(text, [button['action'] for button in buttons])

def render_actions(text, actions):
    buttons = [{'type': 'button', 'style': 'secondary', 'height': 'sm', 'action': action} for action in actions]
    result = {'type': 'flex', 'altText': utf16_limit(text, 400), 'contents': {
        'type': 'bubble', 'body': {'type': 'box', 'layout': 'vertical', 'contents': [
            {'type': 'text', 'text': chunk, 'wrap': True, 'size': 'sm'} for chunk in text_chunks(text)]},
        'footer': {'type': 'box', 'layout': 'vertical', 'spacing': 'sm', 'contents': buttons}}}
    if len(json.dumps(result, ensure_ascii=False, separators=(',', ':')).encode()) > 30000:
        raise ValueError('Complete review exceeds one Flex bubble; split the batch')
    return result
