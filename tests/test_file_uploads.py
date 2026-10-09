import json
import unittest
from fastapi.testclient import TestClient
from backend.main import create_app
from backend.ai import UnconfiguredExplainer


class FileUploadTests(unittest.TestCase):
    def setUp(self):
        self.client = TestClient(create_app(UnconfiguredExplainer()))

    def upload(self, paths, contents, name='demo'):
        return self.client.post('/api/projects/files', data={'paths': json.dumps(paths), 'name': name},
                                files=[('files', ('source', code, 'application/octet-stream')) for code in contents])

    def test_single_file_and_folder_relationships(self):
        response = self.upload(['hello.py'], [b'print("hello")'])
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        file = project['files'][0]
        self.assertEqual(file['path'], 'hello.py')
        source = self.client.get(f"/api/projects/{project['id']}/files/{file['file_id']}")
        self.assertEqual(source.json()['code'], 'print("hello")')
        response = self.upload(['demo/main.py', 'demo/helper.py', 'demo/node_modules/lib.js', 'demo/.env', 'demo/readme.txt'],
                               [b'import helper', b'x=1', b'alert(1)', b'SECRET=excluded', b'notes'])
        self.assertEqual(response.status_code, 201, response.text)
        project = response.json()
        self.assertEqual(project['analyzed_files'], 2)
        self.assertEqual(project['skipped_files'], 3)
        self.assertTrue(any(r['resolved'] and r['target'] == 'demo/helper.py' for r in project['relationships']))

    def test_invalid_paths_and_limits(self):
        cases = [(['../x.py'], [b'x=1']), (['/x.py'], [b'x=1']),
                 (['x.py', 'X.py'], [b'x=1', b'x=2']), (['x.py', 'y.py'], [b'x=1']),
                 (['x.py'], [b'\0']), (['x.txt'], [b'notes']),
                 (['x.py'], [b'x' * (100 * 1024 + 1)]),
                 (['x.py'], [b'x' * (10 * 1024 * 1024 + 1)])]
        for paths, contents in cases:
            with self.subTest(paths=paths, size=len(contents[0])):
                self.assertEqual(self.upload(paths, contents).status_code, 422)
        response = self.client.post('/api/projects/files', data={'paths': 'not json'}, files={'files': ('x.py', b'x=1')})
        self.assertEqual(response.status_code, 422)

    def test_pasted_code_pipeline(self):
        for language, filename, code in [('python', 'snippet.py', 'print(1)'), ('javascript', 'snippet.js', 'const x=1;'),
                                         ('html', 'snippet.html', '<h1>Hello</h1>'), ('css', 'snippet.css', 'h1 {color:red}')]:
            response = self.client.post('/api/projects/snippet', json={'language': language, 'filename': filename, 'code': code})
            self.assertEqual(response.status_code, 201, response.text)
            self.assertEqual(response.json()['files'][0]['language'], language)
        self.assertEqual(self.client.post('/api/projects/snippet', json={'language':'python','filename':'../x.py','code':'x=1'}).status_code,422)
