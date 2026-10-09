"""Session project -> Phase 4.5 analysis engine, through the real host app with a scripted Ollama."""
import asyncio
import io
import json
import os
import unittest
import zipfile
from unittest import mock
import httpx
from fastapi.testclient import TestClient
from backend.ai import OllamaExplainer, UnconfiguredExplainer
from backend.analysis_bridge import AnalysisBridge
from backend.main import create_app
from app.config import ApiSettings, OllamaSettings, ProjectSettings

SHOP = '''"""Shopping cart helpers."""
import math


def total(prices):
    result = 0
    for i in range(1, len(prices)):
        result += prices[i]
    return result


class Cart:
    def __init__(self):
        self.items = []

    def add(self, price):
        self.items.append(price)

    def total(self):
        return total(self.items)
'''
DUPLICATE = 'def helper():\n    return 1\n\ndef helper():\n    return 2\n'
EXPLAIN = {'summary': 'This file adds up prices.', 'analogy': '', 'sections': [{'file_path': 'shop.py', 'start_line': 7, 'end_line': 8, 'title': 'Adding up', 'description': 'It goes through each price.'}],
           'role_in_app': 'It works out the bill.', 'concept_name': 'Loop', 'concept_explanation': 'A loop repeats work.', 'assumptions': []}
TECHNICAL = {'summary': 'Sums a list.', 'sections': [], 'assumptions': []}
DEBUG = {'summary': 'One thing looks suspicious.', 'findings': [{'file_path': 'shop.py', 'evidence': '7 |     for i in range(1, len(prices)):', 'start_line': 7, 'end_line': 7, 'title': 'Skips the first price',
         'category': 'off_by_one', 'severity': 'medium', 'confidence': 'medium', 'problem': 'The loop may skip the first price.', 'what_could_happen': 'The total may be too low.',
         'likely_cause': 'It starts counting at 1.', 'suggestion': 'Start at 0.'}]}


def reply(payload):
    return httpx.Response(200, json={'response': payload if isinstance(payload, str) else json.dumps(payload), 'done': True, 'done_reason': 'stop', 'prompt_eval_count': 700, 'eval_count': 200})


class Ollama:
    """Scripted stand-in for the local Ollama server; the last reply repeats."""
    def __init__(self, *replies):
        self.replies, self.requests = list(replies) or [reply(EXPLAIN)], []

    def __call__(self, request):
        assert request.url.path == '/api/generate', request.url.path
        self.requests.append(json.loads(request.content))
        item = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(item, BaseException):
            raise item
        return item


def make_zip(files):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as archive:
        for path, text in files.items():
            archive.writestr(path, text)
    return buffer.getvalue()


def make_bridge(handler, **options):
    return AnalysisBridge(ollama_settings=OllamaSettings(), api_settings=ApiSettings(), project_settings=ProjectSettings(), transport=httpx.MockTransport(handler), **options)


class BridgeCase(unittest.TestCase):
    def start(self, ollama=None, files=None, **options):
        self.ollama = ollama or Ollama()
        self.client = self.enterContext(TestClient(create_app(UnconfiguredExplainer(), analysis=make_bridge(self.ollama, **options))))
        response = self.client.post('/api/projects/upload', files={'file': ('project.zip', make_zip(files or {'shop.py': SHOP}), 'application/zip')})
        self.assertEqual(response.status_code, 201, response.text)
        self.project = response.json()
        self.ids = {f['path']: f['file_id'] for f in self.project['files']}

    def analyze(self, name='shop.py', project_id=None, **body):
        """POST an analysis for the uploaded file ``name`` (None: send ``body`` as given)."""
        if name and 'file_id' not in body:
            body['file_id'] = self.ids[name]
        return self.client.post(f"/api/projects/{project_id or self.project['project_id']}/analysis", json=body)

    def prompt(self, index=0):
        return self.ollama.requests[index]['prompt']

    def assertError(self, response, status, code):
        """The host's envelope: ``{code, detail}`` (validation errors add ``fields``)."""
        self.assertEqual(response.status_code, status, response.text)
        body = response.json()
        self.assertTrue({'code', 'detail'} <= set(body) <= {'code', 'detail', 'fields'}, body)
        self.assertEqual(body['code'], code)
        return body['detail']


class SelectionMappingTests(BridgeCase):
    def test_selection_inside_a_function_analyses_that_function_and_says_so(self):
        self.start()
        response = self.analyze(intent='explain', start_line=7, end_line=8)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        selection = body['selection']
        self.assertEqual(selection['scope'], 'symbol')
        self.assertEqual(selection['requested'], {'start_line': 7, 'end_line': 8})
        self.assertEqual((selection['analyzed_symbol'], selection['analyzed_lines']), ('total', {'start_line': 5, 'end_line': 9}))
        self.assertTrue(selection['expanded'])
        self.assertIn('lines 7–8', selection['note'])
        self.assertIn("whole function 'total'", selection['note'])
        self.assertEqual(body['limitations'][0], selection['note'])
        self.assertEqual(body['target_symbols'], ['total'])
        self.assertIn('result += prices[i]', self.prompt())
        self.assertEqual((body['project_id'], body['file_id']), (self.project['project_id'], self.ids['shop.py']))

    def test_selecting_exactly_one_whole_function_is_not_an_expansion(self):
        self.start()
        selection = self.analyze(intent='explain', start_line=5, end_line=9).json()['selection']
        self.assertEqual((selection['scope'], selection['analyzed_symbol'], selection['expanded'], selection['note']), ('symbol', 'total', False, None))

    def test_the_smallest_enclosing_unit_wins(self):
        self.start()
        selection = self.analyze(intent='explain', start_line=20, end_line=20).json()['selection']
        self.assertEqual((selection['analyzed_symbol'], selection['analyzed_lines']), ('Cart.total', {'start_line': 19, 'end_line': 20}))

    def test_a_selection_across_methods_analyses_the_class(self):
        self.start()
        selection = self.analyze(intent='explain', start_line=14, end_line=17).json()['selection']
        self.assertEqual((selection['analyzed_symbol'], selection['analyzed_lines']), ('Cart', {'start_line': 12, 'end_line': 20}))
        self.assertIn("whole class 'Cart'", selection['note'])

    def test_a_selection_spanning_two_units_falls_back_to_the_whole_file(self):
        self.start()
        body = self.analyze(intent='explain', start_line=9, end_line=13).json()
        selection = body['selection']
        self.assertEqual((selection['scope'], selection['analyzed_symbol'], selection['analyzed_lines']), ('file', None, {'start_line': 1, 'end_line': 20}))
        self.assertEqual(selection['requested'], {'start_line': 9, 'end_line': 13})
        self.assertTrue(selection['expanded'])
        self.assertIn('not inside a single function or class', selection['note'])
        self.assertEqual(body['target_symbols'], [])

    def test_module_level_lines_fall_back_to_the_whole_file(self):
        self.start()
        selection = self.analyze(intent='explain', start_line=2, end_line=2).json()['selection']
        self.assertEqual((selection['scope'], selection['expanded']), ('file', True))

    def test_a_name_defined_twice_is_not_guessed(self):
        self.start(files={'dup.py': DUPLICATE})
        selection = self.analyze('dup.py', intent='explain', start_line=2, end_line=2).json()['selection']
        self.assertEqual((selection['scope'], selection['analyzed_symbol'], selection['expanded']), ('file', None, True))
        self.assertIn('shares its name', selection['note'])

    def test_a_whole_file_request_has_nothing_to_disclose(self):
        self.start()
        selection = self.analyze(intent='explain').json()['selection']
        self.assertEqual((selection['scope'], selection['requested'], selection['expanded'], selection['note']), ('file', None, False, None))
        self.assertEqual(selection['analyzed_lines'], {'start_line': 1, 'end_line': 20})

    def test_an_explicit_symbol_is_passed_through(self):
        self.start()
        selection = self.analyze(intent='explain', symbol='Cart.add').json()['selection']
        self.assertEqual((selection['scope'], selection['analyzed_symbol'], selection['analyzed_lines']), ('symbol', 'Cart.add', {'start_line': 16, 'end_line': 17}))

    def test_an_overview_needs_no_file(self):
        self.start()
        body = self.analyze(None, intent='overview').json()
        self.assertEqual((body['intent'], body['file_id'], body['selection']['scope'], body['selection']['analyzed_lines']), ('overview', None, 'project', None))


class ContractTests(BridgeCase):
    def test_depth_experienced_is_the_engines_advanced(self):
        self.start(Ollama(reply(TECHNICAL)))
        body = self.analyze(intent='explain', depth='experienced').json()
        self.assertEqual(body['depth'], 'advanced')
        self.assertIn('experienced engineer', self.prompt())

    def test_depths_and_intents_reach_the_model(self):
        self.start(Ollama(reply(TECHNICAL)))
        for depth in ('intermediate', 'advanced'):
            self.assertEqual(self.analyze(intent='explain', depth=depth).json()['depth'], depth)
        self.assertNotIn('never programmed', self.prompt())

    def test_debug_findings_are_checked_against_the_real_source(self):
        self.start(Ollama(reply(DEBUG)))
        response = self.analyze(intent='debug', start_line=5, end_line=9)
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        finding = body['findings'][0]
        self.assertEqual((finding['verification'], finding['file_path'], finding['start_line']), ('source_verified', 'shop.py', 7))
        self.assertIn('for i in range(1, len(prices))', finding['evidence']['source_excerpt'])  # copied by the backend, not the AI
        self.assertIn(body['debug_outcome'], ('possible_problems', 'no_clear_problem'))
        self.assertTrue(body['notice'].startswith('AI-generated'))
        self.assertEqual(body['generation']['model'], 'qwen2.5-coder:3b')

    def test_invented_citations_are_rejected_not_shown(self):
        invented = dict(EXPLAIN, sections=[{'file_path': 'ghost.py', 'start_line': 1, 'end_line': 3, 'title': 'Nope', 'description': 'No such file.'}])
        self.start(Ollama(reply(invented)))
        section = self.analyze(intent='explain').json()['explanations'][0]
        self.assertEqual((section['location_status'], section['file_path'], section['start_line']), ('rejected', None, None))

    def test_uploaded_skipped_files_are_disclosed(self):
        self.start(files={'shop.py': SHOP, 'notes.txt': 'not code'})
        limitations = self.analyze(intent='explain').json()['limitations']
        self.assertTrue(any('1 file(s) were skipped when the project was uploaded' in text for text in limitations), limitations)

    def test_a_file_the_engine_ignores_is_refused_clearly(self):
        self.start(files={'shop.py': SHOP, 'coverage/report.js': 'var a = 1;\n'})
        detail = self.assertError(self.analyze('coverage/report.js', intent='explain'), 422, 'FILE_NOT_ANALYZED')
        self.assertIn('inside a folder CodeSense ignores', detail)
        self.assertNotIn('ignored_directory', detail)
        self.assertEqual(len(self.ollama.requests), 0)

    def test_a_path_the_engine_rejects_is_left_out_not_fatal(self):
        self.start(files={'shop.py': SHOP, 'dots./extra.py': 'x = 1\n'})
        self.assertEqual(len(self.project['files']), 2)  # the host's upload rules accept both
        body = self.analyze(intent='explain').json()
        self.assertTrue(any('1 file(s) were left out' in text for text in body['limitations']), body['limitations'])
        self.assertNotIn('dots./extra.py', json.dumps(body['coverage']))

    def test_health_reports_analysis_readiness(self):
        self.start()
        self.assertEqual(self.client.get('/api/health').json()['analysis'], {'available': True, 'reason': None})

    def test_the_session_is_not_modified(self):
        self.start()
        before = self.client.get(f"/api/projects/{self.project['project_id']}").json()
        self.analyze(intent='explain', start_line=7, end_line=8)
        self.assertEqual(self.client.get(f"/api/projects/{self.project['project_id']}").json(), before)
        self.assertEqual(self.client.get(f"/api/projects/{self.project['project_id']}/files/{self.ids['shop.py']}").json()['code'], SHOP)


class RequestValidationTests(BridgeCase):
    def test_bad_requests_are_422_and_never_reach_the_model(self):
        self.start()
        for body in ({'intent': 'explain'},  # no file
                     {'intent': 'explain', 'file_id': self.ids['shop.py'], 'start_line': 3},  # half a range
                     {'intent': 'explain', 'file_id': self.ids['shop.py'], 'start_line': 9, 'end_line': 3},
                     {'intent': 'explain', 'file_id': self.ids['shop.py'], 'start_line': 3, 'end_line': 4, 'symbol': 'total'},
                     {'intent': 'overview', 'start_line': 1, 'end_line': 2},
                     {'intent': 'explain', 'file_id': self.ids['shop.py'], 'difficulty': 'beginner'},  # unknown field
                     {'intent': 'fix', 'file_id': self.ids['shop.py']},
                     {'intent': 'explain', 'file_id': self.ids['shop.py'], 'depth': 'expert'}):
            response = self.client.post(f"/api/projects/{self.project['project_id']}/analysis", json=body)
            self.assertError(response, 422, 'INVALID_INPUT')
        self.assertEqual(len(self.ollama.requests), 0)

    def test_missing_things_are_404_and_bad_selections_422(self):
        self.start()
        self.assertError(self.analyze(project_id='nope', intent='explain'), 404, 'NOT_FOUND')
        self.assertError(self.analyze(None, intent='explain', file_id='nope'), 404, 'NOT_FOUND')
        self.assertError(self.analyze(intent='explain', start_line=1, end_line=99), 422, 'INVALID_PROJECT')
        self.assertError(self.analyze(intent='explain', path='other.py'), 422, 'INVALID_PROJECT')
        self.assertError(self.analyze(None, intent='explain', path='missing.py'), 422, 'INVALID_PROJECT')
        self.assertError(self.analyze(intent='explain', symbol='nothing_here'), 422, 'SYMBOL_NOT_FOUND')
        self.assertEqual(len(self.ollama.requests), 0)

    def test_a_skipped_file_cannot_be_analysed(self):
        self.start(files={'shop.py': SHOP, 'notes.txt': 'not code'})
        skipped = next(f for f in self.project['files'] if f['status'] == 'skipped')
        self.assertError(self.analyze(None, intent='explain', file_id=skipped['file_id']), 422, 'INVALID_PROJECT')


class FailureMappingTests(BridgeCase):
    def failure(self, outcome, status, code):
        self.start(Ollama(outcome))
        detail = self.assertError(self.analyze(intent='explain'), status, code)
        self.assertNotIn('Traceback', detail)
        return detail

    def test_ollama_down(self):
        self.assertIn('Ollama', self.failure(httpx.ConnectError('refused'), 503, 'OLLAMA_UNAVAILABLE'))
        self.assertEqual(self.client.post(f"/api/projects/{self.project['project_id']}/analysis", json={'intent': 'explain', 'file_id': self.ids['shop.py']}).headers['retry-after'], '5')

    def test_model_not_installed(self):
        self.assertIn('ollama pull', self.failure(httpx.Response(404, json={'error': "model 'qwen2.5-coder:3b' not found"}), 503, 'MODEL_NOT_INSTALLED'))

    def test_ollama_read_timeout(self):
        self.failure(httpx.ReadTimeout('slow'), 504, 'GENERATION_TIMEOUT')

    def test_malformed_model_output(self):
        self.failure(reply('not json at all'), 502, 'INVALID_STRUCTURED_OUTPUT')

    def test_overall_timeout_releases_the_gate(self):
        async def slow(request):
            await asyncio.sleep(2)
            return reply(EXPLAIN)
        bridge = make_bridge(slow, timeout=0.05)
        with TestClient(create_app(UnconfiguredExplainer(), analysis=bridge)) as client:
            project = client.post('/api/projects/upload', files={'file': ('p.zip', make_zip({'shop.py': SHOP}), 'application/zip')}).json()
            body = {'intent': 'explain', 'file_id': project['files'][0]['file_id']}
            self.assertError(client.post(f"/api/projects/{project['project_id']}/analysis", json=body), 504, 'AI_TIMEOUT')
            self.assertFalse(bridge.lock.locked())


class SharedModelSettingsTests(unittest.TestCase):
    def test_both_stacks_use_one_model_and_one_context_window(self):
        settings = OllamaSettings.from_env({'CODESENSE_MODEL': 'older-name:1b'})
        explainer, bridge = OllamaExplainer(settings=settings), make_bridge(Ollama())
        self.assertEqual((explainer.model, explainer.num_ctx), ('older-name:1b', 4096))
        self.assertEqual(bridge.ollama.settings.num_ctx, explainer.num_ctx)

    def test_the_default_app_reads_ollama_model_for_everything(self):
        with mock.patch.dict(os.environ, {'OLLAMA_MODEL': 'env-model:7b', 'CODESENSE_MODEL': 'ignored:1b'}):
            app = create_app()
        self.assertEqual(app.state.explainer.model, 'env-model:7b')
        self.assertEqual(app.state.analysis.ollama_settings.model, 'env-model:7b')
        self.assertIs(app.state.generation_lock, app.state.analysis.lock)

    def test_the_engine_request_uses_the_standard_model_and_context(self):
        ollama = Ollama()
        with TestClient(create_app(UnconfiguredExplainer(), analysis=make_bridge(ollama))) as client:
            project = client.post('/api/projects/upload', files={'file': ('p.zip', make_zip({'shop.py': SHOP}), 'application/zip')}).json()
            client.post(f"/api/projects/{project['project_id']}/analysis", json={'intent': 'explain', 'file_id': project['files'][0]['file_id']})
        self.assertEqual((ollama.requests[0]['model'], ollama.requests[0]['options']['num_ctx']), ('qwen2.5-coder:3b', 4096))

    def test_cloud_models_are_refused_by_the_session_adapter(self):
        with self.assertRaises(ValueError):
            OllamaExplainer(model='something:cloud')


class SharedGateTests(unittest.IsolatedAsyncioTestCase):
    async def test_one_model_request_at_a_time_across_every_route(self):
        started, release = asyncio.Event(), asyncio.Event()

        async def ollama(request):
            started.set()
            await release.wait()
            return reply(EXPLAIN)
        legacy = OllamaExplainer(transport=httpx.MockTransport(lambda request: httpx.Response(200, json={'models': [{'name': 'qwen2.5-coder:3b'}]})), settings=OllamaSettings())
        app = create_app(legacy, analysis=make_bridge(ollama))
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url='http://test') as client:
            project = (await client.post('/api/projects/upload', files={'file': ('p.zip', make_zip({'shop.py': SHOP}), 'application/zip')})).json()
            file_id, url = project['files'][0]['file_id'], f"/api/projects/{project['project_id']}/analysis"
            first = asyncio.create_task(client.post(url, json={'intent': 'explain', 'file_id': file_id}))
            await asyncio.wait_for(started.wait(), 5)
            for other in (client.post(url, json={'intent': 'debug', 'file_id': file_id}),
                          client.post('/api/analyze', json={'project_id': project['project_id'], 'file_id': file_id, 'scope': 'file'}),
                          client.post('/api/challenges/generate', json={'project_id': project['project_id'], 'file_id': file_id})):
                response = await other
                self.assertEqual((response.status_code, response.json()['code']), (429, 'AI_BUSY'))
            release.set()
            self.assertEqual((await first).status_code, 200)
            self.assertEqual((await client.post(url, json={'intent': 'explain', 'file_id': file_id})).status_code, 200)  # gate released


if __name__ == '__main__':
    unittest.main()
