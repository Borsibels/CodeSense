import ast
import posixpath
from bs4 import BeautifulSoup
import tinycss2
from tree_sitter import Language as TSLanguage, Parser
import tree_sitter_javascript
from .models import Construct, SourceFile, Relationship

EXTENSIONS = {'.py': 'python', '.js': 'javascript', '.html': 'html', '.htm': 'html', '.css': 'css'}

def analyze(path: str, code: str, language: str) -> tuple[SourceFile, list[tuple[str, str]]]:
    result = SourceFile(path=path, language=language, status='analyzed', size=len(code.encode('utf-8')))
    refs = []
    concepts = set()
    if language == 'python':
        try:
            tree = ast.parse(code)
        except (SyntaxError, ValueError, RecursionError) as exc:
            result.status = 'partial'
            result.reason = f'Python syntax could not be parsed: {exc}'
            return result, refs
        kinds = {ast.FunctionDef: 'function', ast.AsyncFunctionDef: 'function', ast.ClassDef: 'class', ast.For: 'loop', ast.AsyncFor: 'loop', ast.While: 'loop', ast.If: 'condition', ast.Try: 'exception', ast.Import: 'import', ast.ImportFrom: 'import', ast.Assign: 'assignment', ast.AnnAssign: 'assignment', ast.AugAssign: 'assignment', ast.Return: 'return'}
        for node in ast.walk(tree):
            kind = kinds.get(type(node))
            if kind:
                concepts.add(kind)
                result.constructs.append(Construct(kind=kind, name=getattr(node, 'name', None), start_line=node.lineno, end_line=node.end_lineno or node.lineno))
            if isinstance(node, ast.Import):
                refs.extend(('python_import', item.name) for item in node.names)
            elif isinstance(node, ast.ImportFrom):
                prefix = '.' * node.level + (node.module or '')
                refs.append(('python_import', prefix))
                refs.extend(('python_import', prefix + ('.' if node.module else '') + item.name) for item in node.names if item.name != '*')
    elif language == 'javascript':
        parser = Parser(TSLanguage(tree_sitter_javascript.language()))
        raw = code.encode('utf-8')
        tree = parser.parse(raw)
        if tree.root_node.has_error:
            result.status = 'partial'
            result.reason = 'JavaScript contains syntax errors; extracted structure may be incomplete.'
        kinds = {'function_declaration': 'function', 'function_expression': 'function', 'arrow_function': 'function', 'method_definition': 'function', 'class_declaration': 'class', 'for_statement': 'loop', 'for_in_statement': 'loop', 'while_statement': 'loop', 'do_statement': 'loop', 'if_statement': 'condition', 'try_statement': 'exception', 'import_statement': 'import', 'variable_declarator':'assignment', 'return_statement':'return'}
        pending = [tree.root_node]
        while pending:
            node = pending.pop()
            kind = kinds.get(node.type)
            if kind:
                concepts.add(kind)
                name = node.child_by_field_name('name')
                result.constructs.append(Construct(kind=kind, name=raw[name.start_byte:name.end_byte].decode() if name else None, start_line=node.start_point.row+1, end_line=node.end_point.row+1))
            if node.type == 'import_statement':
                source = node.child_by_field_name('source')
                if source:
                    refs.append(('javascript_import', raw[source.start_byte:source.end_byte].decode()[1:-1]))
            if node.type == 'call_expression':
                fn = node.child_by_field_name('function')
                args = node.child_by_field_name('arguments')
                fn_text = raw[fn.start_byte:fn.end_byte].decode() if fn else ''
                if fn_text.endswith('.addEventListener'):
                    concepts.add('event')
                    result.constructs.append(Construct(kind='event',name=fn_text,start_line=node.start_point.row+1,end_line=node.end_point.row+1))
                if fn_text in ('document.getElementById', 'document.querySelector') and args and args.named_children:
                    arg = args.named_children[0]
                    if arg.type == 'string':
                        value = raw[arg.start_byte:arg.end_byte].decode()[1:-1]
                        if fn_text.endswith('getElementById') or value.startswith('#'):
                            refs.append(('html_id', value.removeprefix('#')))
                            concepts.add('dom')
            pending.extend(reversed(node.children))
    elif language == 'html':
        soup = BeautifulSoup(code, 'html.parser')
        concepts.add('markup')
        for tag in soup.find_all(True):
            line = tag.sourceline or 1
            attributes = {k:v for k,v in tag.attrs.items() if k in ('id','class','type','for','name','href','src','alt')}
            result.constructs.append(Construct(kind='element', name=tag.name, start_line=line, end_line=line,details=attributes))
            if tag.get('id'):
                refs.append(('defines_id', str(tag['id'])))
            if tag.name == 'script' and tag.get('src'):
                refs.append(('script', str(tag['src'])))
            if tag.name == 'link' and 'stylesheet' in tag.get('rel', []) and tag.get('href'):
                refs.append(('stylesheet', str(tag['href'])))
            if tag.name == 'form':
                concepts.add('form')
    elif language == 'css':
        concepts.add('styling')
        rules = tinycss2.parse_stylesheet(code, skip_comments=True, skip_whitespace=True)
        for rule in rules:
            if rule.type == 'error':
                result.status = 'partial'
                result.reason = 'CSS contains parsing errors.'
            elif rule.type == 'qualified-rule':
                name = tinycss2.serialize(rule.prelude).strip()
                result.constructs.append(Construct(kind='selector', name=name, start_line=rule.source_line, end_line=rule.source_line))
                declarations = tinycss2.parse_declaration_list(rule.content,skip_comments=True,skip_whitespace=True)
                for declaration in declarations:
                    if declaration.type == 'declaration':
                        result.constructs.append(Construct(kind='declaration',name=declaration.lower_name,start_line=declaration.source_line,end_line=declaration.source_line,details={'value':tinycss2.serialize(declaration.value).strip(),'important':declaration.important}))
                if any(d.type == 'error' for d in declarations):
                    result.status = 'partial'
                    result.reason = 'CSS contains invalid declarations.'
    result.concepts = sorted(concepts)
    result.constructs.sort(key=lambda item: item.start_line)
    return result, refs

def relationships(files: dict[str, SourceFile], references: dict[str, list[tuple[str, str]]]) -> list[Relationship]:
    output = []
    for path, refs in references.items():
        for kind, value in refs:
            if kind == 'defines_id':
                continue
            candidates = []
            if kind == 'html_id':
                candidates = [p for p, entries in references.items() if ('defines_id', value) in entries]
            elif kind == 'python_import':
                dots = len(value) - len(value.lstrip('.'))
                module = value.lstrip('.').replace('.', '/')
                base = posixpath.dirname(path) if dots else ''
                for _ in range(max(0, dots-1)):
                    base = posixpath.dirname(base)
                target = posixpath.join(base, module)
                candidates = [target+'.py', posixpath.join(target, '__init__.py')]
                if not dots:
                    # ZIPs commonly wrap a project in one enclosing directory.
                    first = path.split('/')[0]
                    if '/' in path and all(p.startswith(first+'/') for p in files):
                        wrapped = posixpath.join(first, module)
                        candidates += [wrapped+'.py', posixpath.join(wrapped, '__init__.py')]
            elif '://' not in value and not value.startswith(('//', 'data:')):
                clean = value.split('?')[0].split('#')[0]
                if kind != 'javascript_import' or clean.startswith('.'):
                    target = posixpath.normpath(posixpath.join(posixpath.dirname(path), clean)) if not clean.startswith('/') else clean.lstrip('/')
                    candidates = [target, target+'.js', posixpath.join(target, 'index.js')]
            matches = [p for p in candidates if p in files and files[p].status != 'skipped']
            for target in matches or [value]:
                output.append(Relationship(source=path, target=target, kind=kind, resolved=bool(matches)))
    return output
