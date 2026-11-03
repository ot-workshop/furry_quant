import os
from types import SimpleNamespace
from pathlib import Path
from fastapi.testclient import TestClient
from finance_quant.application import index_download_manager as manager
from finance_quant.api import create_app

def test_background_start_is_single_and_hidden_and_pause_keeps_data(tmp_path,monkeypatch):
    root=tmp_path/'warehouse';calls=[]
    def launch(command,**kwargs):calls.append((command,kwargs));return SimpleNamespace(pid=os.getpid())
    monkeypatch.setattr(manager.subprocess,'Popen',launch)
    first=manager.start(root);second=manager.start(root)
    assert first['worker_running'] and second['worker_running'] and len(calls)==1
    assert calls[0][0][calls[0][0].index('--data-dir')+1]==str(root)
    assert calls[0][1]['creationflags']==getattr(manager.subprocess,'CREATE_NO_WINDOW',0)
    (root/'cached.parquet').write_bytes(b'capture')
    paused=manager.pause(root)
    assert paused['pause_requested'] and (root/'cached.parquet').read_bytes()==b'capture'

def test_controls_require_local_origin_and_explicit_empty_body(tmp_path,monkeypatch):
    calls=[]
    monkeypatch.setattr(manager,'status',lambda:{'status':'not_started'})
    monkeypatch.setattr(manager,'start',lambda:(calls.append('start') or {'status':'running'}))
    monkeypatch.setattr(manager,'pause',lambda:(calls.append('pause') or {'status':'paused'}))
    client=TestClient(create_app(tmp_path/'runs',data_root=tmp_path/'data'),base_url='http://127.0.0.1')
    assert client.get('/api/v1/research/index-download').json()['status']=='not_started'
    assert not calls
    assert client.post('/api/v1/research/index-download/start',json={},headers={'Origin':'https://outside.test'}).status_code==403
    assert client.post('/api/v1/research/index-download/start',json={'token':'never-accept-this'}).status_code==422
    assert not calls
    assert client.post('/api/v1/research/index-download/start',json={}).status_code==200
    assert client.post('/api/v1/research/index-download/pause',json={}).status_code==200
    assert calls==['start','pause']
