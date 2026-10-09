"""Curated exercises checked by structure, without eval, exec, or subprocesses."""
import ast
import json
from pathlib import Path
import threading
import uuid
import time
from bs4 import BeautifulSoup, NavigableString, Tag
import tinycss2
from tree_sitter import Language as TSLanguage, Parser
import tree_sitter_javascript
from pydantic import BaseModel, Field
from .models import Language
from typing import Literal

Difficulty = Literal['beginner', 'intermediate', 'experienced']

class ChallengeSelect(BaseModel):
    project_id: str | None = None
    file_id: str | None = None
    language: Language | None = None
    concepts: list[str] = Field(default_factory=list, max_length=40)
    difficulty: Difficulty = 'beginner'

class ChallengeSubmit(BaseModel):
    challenge_id: str
    code: str = Field(min_length=1, max_length=10000)

def signature(language, code):
    if '\x00' in code or len(code.encode('utf-8')) > 10000:
        raise ValueError('Answer exceeds the source limit or contains NUL bytes.')
    if language == 'python':
        return ast.dump(ast.parse(code), include_attributes=False)
    if language == 'javascript':
        raw = code.encode('utf-8')
        root = Parser(TSLanguage(tree_sitter_javascript.language())).parse(raw).root_node
        if root.has_error:
            raise ValueError('JavaScript syntax could not be parsed.')
        def walk(node):
            if node.type == 'comment':
                return None
            if not node.children:
                return (node.type, raw[node.start_byte:node.end_byte].decode())
            return (node.type, tuple(v for c in node.children if (v := walk(c)) is not None))
        return walk(root)
    if language == 'html':
        def walk(node):
            if isinstance(node, Tag):
                return (node.name, tuple(sorted((k, tuple(v) if isinstance(v,list) else v) for k,v in node.attrs.items())), tuple(v for c in node.children if (v := walk(c)) is not None))
            if type(node) is NavigableString and str(node).strip():
                return str(node).strip()
            return None
        return walk(BeautifulSoup(code, 'html.parser'))
    rules = tinycss2.parse_stylesheet(code, skip_comments=True, skip_whitespace=True)
    result = []
    def tokens(items):
        return tuple((item.type, tinycss2.serialize([item])) for item in items if item.type not in ('whitespace','comment'))
    for rule in rules:
        if rule.type != 'qualified-rule':
            raise ValueError('Only qualified CSS rules are supported by these exercises.')
        declarations = tinycss2.parse_declaration_list(rule.content, skip_comments=True, skip_whitespace=True)
        if any(d.type != 'declaration' for d in declarations):
            raise ValueError('CSS declarations could not be parsed.')
        selector = ' '.join(tinycss2.serialize(rule.prelude).strip().split())
        result.append((selector, tuple((d.lower_name,tokens(d.value),d.important) for d in declarations)))
    return tuple(result)

class ChallengeEngine:
    def __init__(self):
        path = Path(__file__).resolve().parent.parent / 'challenges' / 'exercises.json'
        self.exercises = {item['id']:item for item in json.loads(path.read_text(encoding='utf-8'))}
        self.expected = {key:signature(item['language'],item['solution']) for key,item in self.exercises.items()}
        # Detect accidental already-correct starter exercises at startup.
        for key,item in self.exercises.items():
            if signature(item['language'],item['starter_code']) == self.expected[key]:
                raise ValueError(f'Exercise {key} has an already-correct starter.')
        self.attempts = {}
        self.generated = {}
        self.lock = threading.Lock()

    def get(self, challenge_id):
        if challenge_id in self.generated:
            item = self.generated[challenge_id]
            if time.monotonic() - item['created'] > 3600:
                self.generated.pop(challenge_id, None)
                self.expected.pop(challenge_id, None)
                raise KeyError(challenge_id)
            return item
        if challenge_id not in self.exercises:
            raise KeyError(challenge_id)
        return self.exercises[challenge_id]

    def public(self, item):
        return {k:v for k,v in item.items() if k not in ('solution','hints','feedback','created')} | {'hint_count':len(item['hints']), 'verification_policy':'Restores the original source structure; equivalent rewrites may be rejected.' if item.get('origin') == 'ai_missing_line' else 'Matches the reviewed solution structure; equivalent algorithms may be rejected.'}

    def select(self, request, languages, concepts):
        candidates = [item for item in self.exercises.values() if item['language'] in languages and item['difficulty'] == request.difficulty]
        if not candidates:
            return {'status':'no_match', 'challenge':None, 'reason':'No verified exercise supports the selected language and difficulty.'}
        def matches(item):
            available = concepts.get(item['language'], []) if isinstance(concepts,dict) else concepts
            return set(item['concepts']) & set(available)
        rank = lambda item: (-len(matches(item)), item['id'])
        item = sorted(candidates, key=rank)[0]
        matched = sorted(matches(item))
        return {'status':'selected', 'challenge':self.public(item), 'match':{'kind':'concept' if matched else 'general', 'concepts':matched}}

    def submit(self, request):
        item = self.get(request.challenge_id)
        try:
            correct = signature(item['language'],request.code) == self.expected[request.challenge_id]
            reason = None if correct else 'Your code does not match the narrowly defined correction. Follow the requested edit and preserve the other statements.'
        except (SyntaxError, ValueError, RecursionError) as exc:
            correct, reason = False, 'Your answer could not be parsed by this exercise validator.'
        attempt_id = str(uuid.uuid4())
        with self.lock:
            if len(self.attempts) >= 100:
                self.attempts.pop(next(iter(self.attempts)))
            self.attempts[attempt_id] = request.challenge_id
        return {'attempt_id':attempt_id, 'challenge_id':request.challenge_id,'correct':correct,'verification':'structural','feedback':item['feedback'] if correct else reason,'limitations':['This checks a constrained exercise, not general program correctness.']}
