"""Fresh-interpreter storage/replay harness with synthetic providers only."""
from datetime import datetime
import json
from pathlib import Path
import socket
import sys

from fastapi.testclient import TestClient
from state_records_hosted import RecordsHosted


def main():
    def deny(*args,**kwargs):raise AssertionError('Synthetic provider protocols only')
    socket.socket.connect=deny;socket.socket.connect_ex=deny
    source,target=map(Path,sys.argv[1:3])
    saved=json.loads(source.read_text())
    api=RecordsHosted();api.records_enabled=False
    api.now=datetime.fromisoformat(saved['now']);api.counter=saved['counter']
    api.google.documents=saved['documents'];api.google.version=saved['version']
    api.calendar.events=saved['calendar'];api.calendar.posts=saved['posts'];api.patches=saved['patches']
    with TestClient(api.app()) as client:
        if saved.get('replay'):
            event=saved['replay'];response=api.send(client,event);status=response.status_code
        else:
            result,event=api.turn(client,'ยืนยัน');status=result.status
    data={'now':api.now.isoformat(),'counter':api.counter,'documents':api.google.documents,'version':api.google.version,
          'calendar':api.calendar.events,'posts':api.calendar.posts,'patches':api.patches,'replay':event}
    target.write_text(json.dumps(data,ensure_ascii=False))
    print(json.dumps({'status':status,'posts':api.calendar.posts,'patches':api.patches,'model_requests':len(api.model_bodies)}))


if __name__=='__main__':main()
