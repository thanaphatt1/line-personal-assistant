"""History container signed startup check: no provider calls or business data."""
import json,os
import httpx,uvicorn
from fastapi.testclient import TestClient
from line_assistant.history_cloud_run import create_history_cloud_run_app,HistoryConfig,HISTORY_KEYS
from line_assistant.pilot_config import PILOT_KEYS
from line_assistant.main import signature

def run_checks(values,*,secret_loader=None):
    config=HistoryConfig.from_mapping(values)
    if not config.history_state_enabled:raise ValueError('Explicit bridge/candidate state required')
    calls=[]
    def deny(request):calls.append(1);raise RuntimeError('No startup provider access')
    app=create_history_cloud_run_app(values,transport=httpx.MockTransport(deny),secret_loader=secret_loader,raw_history_enabled=True)
    secret=app.state.assistant.settings.channel_secret
    with TestClient(app) as client:
        assert client.get('/health').json()=={'status':'ok','mode':'owner_expenses'}
        body=b'{"events":[]}'
        assert client.post('/webhooks/line',content=body,headers={'x-line-signature':signature(body,secret)}).status_code==200
        assert client.post('/webhooks/line',content=body,headers={'x-line-signature':'invalid'}).status_code==401
        other='U'+('b' if config.owner_id=='U'+'a'*32 else 'a')*32
        body=json.dumps({'events':[{'type':'message','webhookEventId':'startup-denied','timestamp':0,
            'source':{'type':'user','userId':other},'message':{'type':'text','id':'startup-denied','text':'startup'}}]}).encode()
        assert client.post('/webhooks/line',content=body,headers={'x-line-signature':signature(body,secret)}).status_code==200
    assert not calls
    return {'status':'passed','provider_requests':0,'history_enabled':config.history_enabled,'state_bridge_enabled':True}

def main():
    try:result=run_checks({k:os.environ[k] for k in PILOT_KEYS|HISTORY_KEYS if k in os.environ})
    except Exception:
        print('production_signed_check_result {"status":"failed"}',flush=True);raise SystemExit(1) from None
    print('production_signed_check_result '+json.dumps(result),flush=True)
    # Same reviewed server settings with a separately selected factory.
    port=os.environ.get('PORT','8080')
    if not port.isdigit() or not 1<=int(port)<=65535:raise SystemExit('Invalid PORT')
    uvicorn.run('line_assistant.history_cloud_run:app_factory',factory=True,host='0.0.0.0',port=int(port),workers=1,access_log=False,log_level='warning')

if __name__=='__main__':main()
