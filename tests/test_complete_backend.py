import asyncio
import json
import unittest
import httpx
from fastapi.testclient import TestClient
from backend.ai import OllamaExplainer, UnconfiguredExplainer, build_prompt
from backend.challenges import ChallengeEngine, ChallengeSelect, ChallengeSubmit, signature
from backend.main import create_app
from backend.models import ContextRequest
from backend.projects import ingest_snippet, select_context, store

class FullBackendTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(UnconfiguredExplainer()))
        self.project = self.client.post('/api/projects/snippet',json={'code':'def add(a, b):\n    return a + b','language':'python','filename':'demo/main.py'}).json()
        self.project_id = self.project['project_id']
        self.file_id = self.project['files'][0]['file_id']

    def test_ids_tree_metadata_and_source(self):
        project = self.client.get('/api/projects/'+self.project_id).json()
        self.assertEqual(project['files'][0]['file_id'],self.file_id)
        self.assertEqual(project['file_tree'][0]['children'][0]['file_id'],self.file_id)
        self.assertEqual(project['language_counts'],{'python':1})
        self.assertEqual(project['entry_points'],[self.file_id])
        self.assertEqual(project['analyzed_files']+project['partial_files']+project['skipped_files'],project['total_files'])
        response = self.client.get(f'/api/projects/{self.project_id}/files/{self.file_id}')
        self.assertEqual(response.status_code,200)
        self.assertIn('return a + b',response.json()['code'])
        self.assertEqual(self.client.get(f'/api/projects/{self.project_id}/files/missing').status_code,404)

    def test_scopes_and_validation(self):
        for scope in ('project','file','block'):
            body = {'project_id':self.project_id,'scope':scope}
            if scope != 'project':
                body['file_id'] = self.file_id
            if scope == 'block':
                body.update(start_line=2,end_line=2)
            result = self.client.post('/api/context',json=body)
            self.assertEqual(result.status_code,200,result.text)
            self.assertEqual(result.json()['scope'],scope)
        for extra in ({'scope':'project','file_id':self.file_id},{'scope':'block','file_id':self.file_id},{'file_id':self.file_id,'start_line':3,'end_line':1}):
            self.assertEqual(self.client.post('/api/context',json={'project_id':self.project_id,**extra}).status_code,422)

    def test_all_challenges_reference_and_starter(self):
        exercises = self.client.get('/api/challenges').json()
        self.assertEqual(len(exercises),12)
        self.assertEqual({(e['language'],e['difficulty']) for e in exercises}, {(l,d) for l in ('python','javascript','html','css') for d in ('beginner','intermediate','experienced')})
        for item in exercises:
            with self.subTest(challenge=item['id']):
                self.assertNotIn('solution',item)
                bad = self.client.post('/api/challenges/submit',json={'challenge_id':item['id'],'code':item['starter_code']})
                self.assertFalse(bad.json()['correct'])
                answer = self.client.get(f"/api/challenges/{item['id']}/solution").json()['solution']
                good = self.client.post('/api/challenges/submit',json={'challenge_id':item['id'],'code':answer})
                self.assertTrue(good.json()['correct'],good.text)

    def test_challenge_context_hints_and_validation(self):
        selected = self.client.post('/api/challenges/select',json={'project_id':self.project_id,'file_id':self.file_id,'difficulty':'intermediate'})
        self.assertEqual(selected.status_code,200)
        item = selected.json()['challenge']
        self.assertEqual(item['language'],'python')
        hints = self.client.get(f"/api/challenges/{item['id']}/hints/2").json()
        self.assertEqual(len(hints['hints']),2)
        self.assertEqual(hints['remaining'],1)
        self.assertEqual(self.client.get(f"/api/challenges/{item['id']}/hints/4").status_code,422)
        self.assertEqual(self.client.post('/api/challenges/submit',json={'challenge_id':'missing','code':'x=1'}).status_code,404)
        result = self.client.post('/api/challenges/select',json={'language':'python','concepts':['unrecognized'],'difficulty':'beginner'}).json()
        self.assertEqual(result['match']['kind'],'general')

    def test_challenge_concepts_do_not_leak_between_languages(self):
        engine = ChallengeEngine()
        result = engine.select(ChallengeSelect(difficulty='beginner'), ['python','javascript'], {'python':['condition'],'javascript':['function']})
        self.assertEqual(result['challenge']['language'],'python')
        self.assertEqual(result['match']['concepts'],['condition'])

    def test_structural_validator_rejects_extra_statements_and_wrong_units(self):
        engine = ChallengeEngine()
        item = engine.get('py-condition-001')
        correct = item['solution']+'\nprint("unrequested")'
        self.assertFalse(engine.submit(ChallengeSubmit(challenge_id=item['id'],code=correct))['correct'])
        css = engine.get('css-box-002')['solution'].replace('200px','200em')
        self.assertFalse(engine.submit(ChallengeSubmit(challenge_id='css-box-002',code=css))['correct'])
        formatted = '# Comment\n'+item['solution'].replace('score >= 50','score  >=  50')
        self.assertTrue(engine.submit(ChallengeSubmit(challenge_id=item['id'],code=formatted))['correct'])

    def test_error_contract_and_health(self):
        self.assertEqual(self.client.get('/api/health').json()['ai']['status'],'unconfigured')
        self.assertEqual(self.client.get('/api/projects/missing').json()['code'],'NOT_FOUND')
        response = self.client.post('/api/projects/snippet',json={'code':'private source','language':'unsupported'})
        self.assertEqual(response.json()['code'],'INVALID_INPUT')
        self.assertNotIn('private source',response.text)

    def test_model_prompt_is_bounded(self):
        project = ingest_snippet('x=1\n'*1000,'python','long.py')
        context = select_context(ContextRequest(project_id=project.id,file_id=project.files[0].file_id))
        messages, schema, bounded = build_prompt(context)
        self.assertLessEqual(len(json.dumps({'messages': messages, 'format': schema}, ensure_ascii=False).encode('utf-8')),6000)
        self.assertTrue(bounded.truncated)
        self.assertEqual(bounded.selected.end_line,len(bounded.selected.code.splitlines()))

    def test_ai_full_endpoint_with_mock_local_http(self):
        def ollama(request):
            if request.url.path == '/api/tags':
                return httpx.Response(200,json={'models':[{'name':'qwen2.5-coder:3b'}]})
            payload = json.loads(request.content)
            self.assertEqual(str(request.url),'http://127.0.0.1:11434/api/chat')
            self.assertEqual(payload['options']['num_ctx'],4096)
            self.assertFalse(payload['stream'])
            context = json.loads(payload['messages'][1]['content'])
            selected = context['selected']
            result = {'overview':'Adds two values.','sections':[{'file_id':selected['file_id'],'title':'Return','explanation':'Returns the sum.','start_line':2,'end_line':2}], 'concepts':['function'],'limitations':[]}
            return httpx.Response(200,json={'message':{'content':json.dumps(result)},'done_reason':'stop'})
        client = TestClient(create_app(OllamaExplainer(transport=httpx.MockTransport(ollama))))
        body = {'project_id':self.project_id,'file_id':self.file_id,'scope':'file'}
        response = client.post('/api/analyze',json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['source_references'][0]['file_id'],self.file_id)
        self.assertEqual(client.get('/api/health').json()['ai']['status'],'ready')

    def test_ai_missing_model_and_invalid_json(self):
        def missing(request):
            return httpx.Response(200,json={'models':[]})
        client = TestClient(create_app(OllamaExplainer(transport=httpx.MockTransport(missing))))
        body = {'project_id':self.project_id,'file_id':self.file_id}
        self.assertEqual(client.get('/api/health').json()['ai']['status'],'model_missing')
        self.assertEqual(client.post('/api/analyze',json=body).status_code,503)
        def malformed(request):
            if request.url.path == '/api/tags':
                return httpx.Response(200,json={'models':[{'name':'qwen2.5-coder:3b'}]})
            return httpx.Response(200,json={'message':{'content':'not json'}})
        client = TestClient(create_app(OllamaExplainer(transport=httpx.MockTransport(malformed))))
        self.assertEqual(client.post('/api/analyze',json=body).status_code,502)

    def test_timeout_and_lock_recovery(self):
        class SlowExplainer:
            async def explain(self, context):
                await asyncio.sleep(1)
        app = create_app(SlowExplainer(),inference_timeout=0.01)
        with TestClient(app) as client:
            body = {'project_id':self.project_id,'file_id':self.file_id}
            self.assertEqual(client.post('/api/analyze',json=body).status_code,504)
            self.assertFalse(app.state.generation_lock.locked())
            self.assertEqual(client.post('/api/analyze',json=body).status_code,504)

class ConcurrencyTests(unittest.IsolatedAsyncioTestCase):
    async def test_second_generation_is_rejected_and_lock_recovers(self):
        entered, release = asyncio.Event(), asyncio.Event()
        class BlockingExplainer:
            async def explain(self, context):
                entered.set()
                await release.wait()
                return {'overview':'Stores one.','sections':[{'title':'Assignment','explanation':'Stores one as x.','start_line':1,'end_line':1}]}
        app = create_app(BlockingExplainer())
        project = ingest_snippet('x=1','python','main.py')
        body = {'project_id':project.id,'file_id':project.files[0].file_id}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),base_url='http://localhost') as client:
            first = asyncio.create_task(client.post('/api/analyze',json=body))
            await asyncio.wait_for(entered.wait(),2)
            second = await client.post('/api/analyze',json=body)
            self.assertEqual(second.status_code,429)
            self.assertEqual(second.json()['code'],'AI_BUSY')
            release.set()
            self.assertEqual((await first).status_code,200)
            self.assertFalse(app.state.generation_lock.locked())
