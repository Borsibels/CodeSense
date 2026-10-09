import json
import unittest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.ai import UnconfiguredExplainer, OllamaExplainer
import httpx

class FakeAI:
    def __init__(self, line=2, hints=None):
        self.line = line
        self.hints = hints or ['Consider what the function should produce.', 'Look at the value that leaves the function.', 'Restore the expression using both parameters.']
        self.requests = []
    async def generate_json(self, messages, schema):
        self.requests.append(json.loads(messages[1]['content']))
        return {'line_number': self.line, 'hints': self.hints}

class MissingLineTests(unittest.TestCase):
    def setup_project(self, adapter):
        self.client = TestClient(create_app(adapter))
        self.original = 'def add(a, b):\n    return a + b\n'
        self.project = self.client.post('/api/projects/snippet',json={'code':self.original,'language':'python','filename':'main.py'}).json()
        return {'project_id':self.project['id'],'file_id':self.project['files'][0]['file_id']}

    def test_full_challenge_pipeline_retains_original_and_hides_answer(self):
        ai = FakeAI()
        body = self.setup_project(ai)
        response = self.client.post('/api/challenges/generate',json=body)
        self.assertEqual(response.status_code,200,response.text)
        item = response.json()['challenge']
        self.assertEqual(item['missing_line'],2)
        self.assertEqual(item['origin'],'ai_missing_line')
        self.assertNotIn('return a + b',item['starter_code'])
        for field in ['solution','hints','feedback','created']:
            self.assertNotIn(field,item)
        source = self.client.get(f"/api/projects/{self.project['id']}/files/{item['source_file_id']}").json()['code']
        self.assertEqual(source,self.original)
        submit = lambda code: self.client.post('/api/challenges/submit',json={'challenge_id':item['id'],'code':code}).json()
        self.assertFalse(submit(item['starter_code'])['correct'])
        self.assertTrue(submit(self.original)['correct'])
        self.assertFalse(submit(self.original.replace('a + b','a - b'))['correct'])
        hints = self.client.get(f"/api/challenges/{item['id']}/hints/1").json()
        self.assertEqual(hints['hints'],ai.hints[:1])
        self.assertEqual(hints['remaining'],2)
        solution = self.client.get(f"/api/challenges/{item['id']}/solution").json()
        self.assertEqual(solution['solution'],self.original)

    def test_difficulty_is_supplied_to_ai_and_requests_create_distinct_sessions(self):
        ai = FakeAI(); body = self.setup_project(ai)
        ids = []
        for level in ['beginner','intermediate','experienced']:
            response = self.client.post('/api/challenges/generate',json=body|{'difficulty':level})
            self.assertEqual(response.status_code,200,response.text)
            self.assertEqual(response.json()['challenge']['difficulty'],level)
            self.assertEqual(ai.requests[-1]['difficulty'],level)
            ids.append(response.json()['challenge']['id'])
        self.assertEqual(len(set(ids)),3)

    def test_invalid_ai_choice_and_leaking_hints_are_rejected(self):
        for ai in [FakeAI(line=999),FakeAI(hints=['return a + b','Second hint','Third hint'])]:
            body = self.setup_project(ai)
            self.assertEqual(self.client.post('/api/challenges/generate',json=body).status_code,502)

    def test_unavailable_ai_and_oversized_source_do_not_fall_back(self):
        body = self.setup_project(UnconfiguredExplainer())
        self.assertEqual(self.client.post('/api/challenges/generate',json=body).status_code,503)
        project = self.client.post('/api/projects/snippet',json={'code':'x=1\n'+'# comment\n'*1000,'language':'python','filename':'large.py'}).json()
        self.assertEqual(self.client.post('/api/challenges/generate',json={'project_id':project['id']}).status_code,422)

    def test_real_ollama_adapter_contract_with_mock_http(self):
        def handler(request):
            if request.url.path == '/api/tags':
                return httpx.Response(200,json={'models':[{'name':'qwen2.5-coder:3b'}]})
            self.assertEqual(request.url.path,'/api/chat')
            payload = json.loads(request.content)
            self.assertIn('line_number',payload['format']['properties'])
            return httpx.Response(200,json={'message':{'content':json.dumps({'line_number':2,'hints':['Consider the output.','Look at the parameters.','Restore their arithmetic expression.']})}})
        body = self.setup_project(OllamaExplainer(transport=httpx.MockTransport(handler)))
        response = self.client.post('/api/challenges/generate',json=body)
        self.assertEqual(response.status_code,200,response.text)
