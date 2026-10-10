import json
import unittest
import httpx
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.ai import OllamaExplainer, UnconfiguredExplainer, prompt_fits
from backend.qa import QuestionRequest, build_question_prompt, scope_key

class QATests(unittest.TestCase):
    def setup_client(self, adapter):
        client = TestClient(create_app(adapter, inference_timeout=0.03))
        project = client.post('/api/projects/snippet', json={'code':'def total(prices):\n    return sum(prices)\n','language':'python','filename':'total.py'}).json()
        body = {'project_id':project['project_id'],'file_id':project['files'][0]['file_id'],'scope':'file','question':'What does sum do?','difficulty':'beginner'}
        return client, body

    def test_real_adapter_uses_local_http_and_selected_source(self):
        seen = []
        def ollama(request):
            if request.url.path == '/api/tags': return httpx.Response(200,json={'models':[{'name':'qwen2.5-coder:3b'}]})
            payload = json.loads(request.content); seen.append(payload)
            self.assertEqual(request.url.host,'127.0.0.1')
            context = json.loads(payload['messages'][1]['content'])
            self.assertEqual(context['question'],'What does sum do?')
            self.assertIn('return sum(prices)',context['context']['selected']['code'])
            return httpx.Response(200,json={'message':{'content':json.dumps({'answer':'It adds the prices.','limitations':[]})}})
        client, body = self.setup_client(OllamaExplainer(transport=httpx.MockTransport(ollama)))
        before = client.get(f"/api/projects/{body['project_id']}/files/{body['file_id']}").json()
        response = client.post('/api/questions',json=body)
        self.assertEqual(response.status_code,200,response.text)
        self.assertEqual(response.json()['answer'],'It adds the prices.')
        self.assertEqual(client.get(f"/api/projects/{body['project_id']}/files/{body['file_id']}").json(),before)
        self.assertEqual(len(seen),1)

    def test_unavailable_and_validation(self):
        client, body = self.setup_client(UnconfiguredExplainer())
        self.assertEqual(client.post('/api/questions',json=body).status_code,503)
        for question in ['   ','x'*1001]:
            self.assertEqual(client.post('/api/questions',json=body | {'question':question}).status_code,422)
        self.assertEqual(client.post('/api/questions',json=body | {'file_id':'missing'}).status_code,404)
        self.assertEqual(client.post('/api/questions',json=body | {'project_id':'missing'}).status_code,404)

    def test_invalid_answer_timeout_and_lock_recovery(self):
        class Adapter:
            async def generate_json(self, messages, schema): return {'answer':'','limitations':[]}
        client, body = self.setup_client(Adapter())
        self.assertEqual(client.post('/api/questions',json=body).status_code,502)
        self.assertFalse(client.app.state.generation_lock.locked())
        import asyncio
        class Slow:
            async def generate_json(self, messages, schema): await asyncio.sleep(1)
        client.app.state.explainer = Slow()
        self.assertEqual(client.post('/api/questions',json=body).status_code,504)
        self.assertFalse(client.app.state.generation_lock.locked())

    def test_shared_busy_gate(self):
        client, body = self.setup_client(UnconfiguredExplainer())
        class Busy:
            def locked(self): return True
        client.app.state.generation_lock = Busy()
        self.assertEqual(client.post('/api/questions',json=body).status_code,429)

    def test_large_source_is_bounded_and_selection_preserves_line_numbers(self):
        adapter = OllamaExplainer()
        client, body = self.setup_client(adapter)
        project = client.post('/api/projects/snippet',json={'code':'x = 1\n'*1000,'language':'python'}).json()
        req = QuestionRequest(**(body | {'project_id':project['project_id'],'file_id':project['files'][0]['file_id']}))
        messages, schema, context = build_question_prompt(req,adapter.budget)
        self.assertTrue(prompt_fits(adapter.budget,messages,schema))
        self.assertTrue(context.truncated)
        req = QuestionRequest(**(body | {'scope':'block','start_line':2,'end_line':2}))
        _, _, context = build_question_prompt(req,adapter.budget)
        self.assertEqual(context.selected.start_line,2)
        self.assertEqual(context.selected.code.strip(),'return sum(prices)')


    def test_three_turns_and_initial_explanation_reach_the_real_adapter(self):
        seen = []
        def ollama(request):
            if request.url.path == '/api/tags': return httpx.Response(200, json={'models': [{'name': 'qwen2.5-coder:3b'}]})
            payload = json.loads(request.content); seen.append(payload['messages'])
            return httpx.Response(200, json={'message': {'content': json.dumps({'answer': 'A bounded answer.', 'limitations': []})}})
        client, body = self.setup_client(OllamaExplainer(transport=httpx.MockTransport(ollama)))
        key = scope_key(QuestionRequest(**body))
        history = []
        for q in ['What does sum do?', 'Why use it?', 'Can you explain that again?']:
            response = client.post('/api/questions', json=body | {'question': q, 'history_scope': key,
                'history': history, 'previous_explanation': 'The function adds all prices.'})
            self.assertEqual(response.status_code, 200, response.text)
            history.append({'question': q, 'answer': response.json()['answer']})
        self.assertIn('The function adds all prices.', seen[0][1]['content'])
        self.assertEqual([m['role'] for m in seen[2]], ['system', 'user', 'user', 'assistant', 'user', 'assistant', 'user'])
        self.assertEqual(seen[2][-3]['content'], 'Why use it?')
        self.assertEqual(seen[2][-2]['content'], 'A bounded answer.')
        self.assertIn('return sum(prices)', seen[2][-1]['content'])

    def test_history_scope_is_required_and_rejects_other_projects_files_and_lines(self):
        client, body = self.setup_client(UnconfiguredExplainer())
        history = [{'question': 'What?', 'answer': 'Earlier answer'}]
        self.assertEqual(client.post('/api/questions', json=body | {'history': history}).status_code, 422)
        key = scope_key(QuestionRequest(**body))
        for update in [{'difficulty': 'experienced'}, {'scope': 'block', 'start_line': 2, 'end_line': 2}, {'file_id': 'other'}, {'project_id': 'other'}]:
            response = client.post('/api/questions', json=body | update | {'history': history, 'history_scope': key})
            self.assertEqual(response.status_code, 422, response.text)

    def test_project_questions_include_bounded_original_source(self):
        client, body = self.setup_client(UnconfiguredExplainer())
        req = QuestionRequest(project_id=body['project_id'], scope='project', question='How does the app work?')
        messages, schema, context = build_question_prompt(req, OllamaExplainer().budget)
        self.assertTrue(context.related)
        self.assertIn('return sum(prices)', context.related[0].code)
        self.assertTrue(prompt_fits(OllamaExplainer().budget, messages, schema))

    def test_source_references_are_checked_against_the_context_actually_shown(self):
        class Adapter:
            async def generate_json(self, messages, schema):
                return {'answer': 'A source claim.', 'limitations': [], 'references': [
                    {'path': 'total.py', 'start_line': 2, 'end_line': 2},
                    {'path': 'total.py', 'start_line': 1, 'end_line': 1},
                    {'path': 'missing.py', 'start_line': 1, 'end_line': 2}]}
        client, body = self.setup_client(Adapter())
        response = client.post('/api/questions', json=body | {'scope': 'block', 'start_line': 2, 'end_line': 2})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['references'], [{'path': 'total.py', 'start_line': 2, 'end_line': 2}])
        self.assertEqual(response.json()['context_used'], response.json()['references'])
        self.assertIn('Unverified AI source references were removed.', response.json()['limitations'])

    def test_large_history_is_bounded_before_model_submission(self):
        client, body = self.setup_client(UnconfiguredExplainer())
        key = scope_key(QuestionRequest(**body))
        req = QuestionRequest(**(body | {'history_scope': key, 'history': [
            {'question': 'q'*1000, 'answer': 'answer '*850} for _ in range(4)], 'previous_explanation': 'p'*2000}))
        messages, schema, context = build_question_prompt(req, OllamaExplainer().budget)
        self.assertTrue(prompt_fits(OllamaExplainer().budget, messages, schema))
        self.assertEqual(context.selected.code.splitlines()[-1].strip(), 'return sum(prices)')
        self.assertIn('Only bounded recent conversation excerpts are included.', context.limitations)
