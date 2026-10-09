import io
import unittest
import zipfile
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.models import Explanation
from backend.ai import UnconfiguredExplainer
from backend.projects import ProjectStore

def archive(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, 'w', zipfile.ZIP_DEFLATED) as z:
        for name, text in entries:
            z.writestr(name, text)
    return buffer.getvalue()

class BackendTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(UnconfiguredExplainer()))

    def upload(self, entries):
        return self.client.post('/api/projects/upload', files={'file': ('project.zip', archive(entries), 'application/zip')})

    def test_project_pipeline_all_languages(self):
        response = self.upload([
            ('index.html', '<link rel="stylesheet" href="style.css"><button id="save">Save</button><script src="app.js"></script>'),
            ('style.css', 'button { color: red; }'),
            ('app.js', 'function save() { return document.getElementById("save"); }'),
            ('main.py', 'from helper import greet\nprint(greet())'),
            ('helper.py', 'def greet():\n    return "hello"'),
            ('node_modules/vendor.js', 'alert(1)'),
            ('image.png', b'\x00binary')])
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        files = {f['path']: f for f in project['files']}
        self.assertEqual(files['node_modules/vendor.js']['status'], 'skipped')
        self.assertEqual(files['image.png']['status'], 'skipped')
        self.assertEqual({f['language'] for f in files.values() if f['status']=='analyzed'}, {'python','javascript','html','css'})
        self.assertTrue(any(r['source']=='main.py' and r['target']=='helper.py' and r['resolved'] for r in project['relationships']))
        self.assertTrue(any(r['kind']=='html_id' and r['resolved'] for r in project['relationships']))
        body = {'project_id':project['id'], 'path':'app.js'}
        context = self.client.post('/api/context', json=body)
        self.assertEqual(context.status_code, 200)
        self.assertTrue(context.json()['related'])
        self.assertEqual(self.client.post('/api/analyze', json=body).status_code, 503)
        self.assertEqual(self.client.get(f"/api/projects/{project['id']}/source", params={'path':'app.js'}).status_code, 200)
        self.assertEqual(self.client.delete(f"/api/projects/{project['id']}").status_code, 204)
        self.assertEqual(self.client.get(f"/api/projects/{project['id']}").status_code, 404)

    def test_archive_rejections(self):
        for name in ['../escape.py', '/absolute.py', 'C:/escape.py', 'a/../../escape.py', 'a\\..\\escape.py']:
            with self.subTest(name=name):
                self.assertEqual(self.upload([(name, 'print(1)')]).status_code, 422)
        self.assertEqual(self.upload([('a.py',''),('A.py','')]).status_code,422)
        self.assertEqual(self.client.post('/api/projects/upload', files={'file':('bad.zip',b'bad')}).status_code,422)
        entry = zipfile.ZipInfo('link.py')
        entry.external_attr = 0o120777 << 16
        self.assertEqual(self.upload([(entry,'target')]).status_code,422)

    def test_transport_limit(self):
        response = self.client.post('/api/projects/upload', content=b'x'*(11*1024*1024+1), headers={'Content-Type':'application/octet-stream'})
        self.assertEqual(response.status_code,413)

    def test_wrapped_project_import(self):
        result = self.upload([('demo/main.py','import helper'),('demo/helper.py','x=1')]).json()
        self.assertTrue(any(r['resolved'] and r['target']=='demo/helper.py' for r in result['relationships']))

    def test_limits_and_binary(self):
        response = self.upload([(f'f{i}.py','x=1') for i in range(31)]+[('huge.py','x'*102401),('binary.js',b'\x00')])
        self.assertEqual(response.status_code,201)
        files = response.json()['files']
        self.assertEqual(sum(f['status']=='analyzed' for f in files),30)
        self.assertEqual(self.upload([(str(i)+'.txt','') for i in range(1001)]).status_code,422)
        binary = self.upload([('binary.js',b'\x00'),('valid.py','x=1')]).json()['files'][0]
        self.assertEqual(binary['status'],'skipped')

    def test_invalid_syntax_and_ranges(self):
        project = self.client.post('/api/projects/snippet',json={'code':'def broken(', 'language':'python'}).json()
        self.assertEqual(project['files'][0]['status'],'partial')
        body = {'project_id':project['id'],'path':'snippet','start_line':2}
        self.assertEqual(self.client.post('/api/context',json=body).status_code,422)
        self.assertEqual(self.client.post('/api/projects/snippet',json={'code':'  ','language':'python'}).status_code,422)

    def test_context_budget_and_no_execution(self):
        code = 'raise RuntimeError("must never run")\n' + ('x = 1\n'*2000)
        project = self.client.post('/api/projects/snippet',json={'code':code,'language':'python'}).json()
        result = self.client.post('/api/context',json={'project_id':project['id'],'path':'snippet'}).json()
        self.assertTrue(result['truncated'])
        self.assertLessEqual(len(result['selected']['code'].encode()),8000)

    def test_ai_contract_validation(self):
        class FakeExplainer:
            async def explain(self, context):
                return Explanation(overview='Stores a number.',sections=[{'title':'Assignment','explanation':'Saves one as x.','start_line':1,'end_line':1}])
        client = TestClient(create_app(FakeExplainer()))
        project = client.post('/api/projects/snippet',json={'code':'x=1','language':'python'}).json()
        body = {'project_id':project['id'],'path':'snippet'}
        self.assertEqual(client.post('/api/analyze',json=body).status_code,200)
        class BadExplainer:
            async def explain(self, context):
                return {'overview':'bad','sections':[{'title':'bad','explanation':'bad','start_line':99,'end_line':99}]}
        with TestClient(create_app(BadExplainer())) as other:
            self.assertEqual(other.post('/api/analyze',json=body).status_code,502)

    def test_store_is_bounded(self):
        from backend.models import Project
        store = ProjectStore()
        for i in range(9):
            store.put(Project(id=str(i),name='demo',files=[],relationships=[]),{})
        with self.assertRaises(KeyError):
            store.get('0')
        self.assertEqual(store.get('8')[0].id,'8')

if __name__ == '__main__':
    unittest.main()
