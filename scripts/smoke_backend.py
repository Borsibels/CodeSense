"""Start a real loopback server, verify the local workflow, and always stop it."""
import io
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import time
import zipfile
import httpx

root = Path(__file__).resolve().parent.parent
with socket.socket() as probe:
    probe.bind(('127.0.0.1',0))
    port = probe.getsockname()[1]
process = subprocess.Popen([sys.executable,'-m','uvicorn','backend.main:app','--host','127.0.0.1','--port',str(port)],cwd=root,stdout=subprocess.DEVNULL,stderr=subprocess.PIPE,creationflags=subprocess.CREATE_NO_WINDOW if os.name=='nt' else 0)
try:
    with httpx.Client(base_url=f'http://127.0.0.1:{port}',timeout=100,trust_env=False) as client:
        health = None
        for _ in range(50):
            try:
                response = client.get('/api/health')
                response.raise_for_status()
                health = response.json()
                break
            except httpx.HTTPError:
                if process.poll() is not None:
                    raise RuntimeError(process.stderr.read().decode())
                time.sleep(0.1)
        assert health is not None, 'Backend did not start.'
        buffer = io.BytesIO()
        with zipfile.ZipFile(buffer,'w') as archive:
            archive.writestr('index.html','<script src="app.js"></script>')
            archive.writestr('app.js','function isPassing(score) { return score > 50; }')
        response = client.post('/api/projects/upload',files={'file':('demo.zip',buffer.getvalue(),'application/zip')})
        response.raise_for_status()
        project = response.json()
        file = next(f for f in project['files'] if f['path']=='app.js')
        pid = project['project_id']
        source = client.get(f"/api/projects/{pid}/files/{file['file_id']}")
        source.raise_for_status()
        body = {'project_id':pid,'file_id':file['file_id'],'scope':'file'}
        client.post('/api/context',json=body).raise_for_status()
        selected = client.post('/api/challenges/select',json={'project_id':pid,'file_id':file['file_id']}).json()
        challenge = selected['challenge']
        bad = client.post('/api/challenges/submit',json={'challenge_id':challenge['id'],'code':challenge['starter_code']}).json()
        assert not bad['correct']
        client.get(f"/api/challenges/{challenge['id']}/hints/1").raise_for_status()
        solution = client.get(f"/api/challenges/{challenge['id']}/solution").json()['solution']
        good = client.post('/api/challenges/submit',json={'challenge_id':challenge['id'],'code':solution}).json()
        assert good['correct']
        ai_verified = False
        if health['ai']['status'] == 'ready':
            result = client.post('/api/analyze',json=body)
            result.raise_for_status()
            assert result.json()['summary']
            ai_verified = True
        else:
            assert client.post('/api/analyze',json=body).status_code == 503
        client.delete(f'/api/projects/{pid}').raise_for_status()
        assert client.get(f'/api/projects/{pid}').status_code == 404
        print(json.dumps({'backend_workflow':'passed','ai':health['ai'],'live_ai_generation_verified':ai_verified}))
finally:
    process.terminate()
    try:
        process.wait(timeout=5)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait()
    if process.stderr:
        process.stderr.close()
