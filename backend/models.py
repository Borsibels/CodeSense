from typing import Literal
from pydantic import BaseModel, Field, computed_field, model_validator

Language = Literal['python', 'javascript', 'html', 'css']

class Construct(BaseModel):
    kind: str
    name: str | None = None
    start_line: int
    end_line: int
    details: dict = Field(default_factory=dict)

class SourceFile(BaseModel):
    file_id: str = ''
    path: str
    language: Language | None = None
    status: Literal['analyzed', 'partial', 'skipped']
    reason: str | None = None
    size: int
    constructs: list[Construct] = Field(default_factory=list)
    concepts: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def size_bytes(self) -> int:
        return self.size

class Relationship(BaseModel):
    source_id: str | None = None
    target_id: str | None = None
    source: str
    target: str
    kind: str
    resolved: bool

class FileTreeNode(BaseModel):
    name: str
    path: str
    type: Literal['directory', 'file']
    file_id: str | None = None
    status: str | None = None
    children: list['FileTreeNode'] = Field(default_factory=list)

class Project(BaseModel):
    id: str
    name: str
    files: list[SourceFile]
    relationships: list[Relationship]
    file_tree: list[FileTreeNode] = Field(default_factory=list)
    entry_points: list[str] = Field(default_factory=list)
    language_counts: dict[str, int] = Field(default_factory=dict)
    warnings: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def project_id(self) -> str:
        return self.id

    @computed_field
    @property
    def total_files(self) -> int:
        return len(self.files)

    @computed_field
    @property
    def analyzed_files(self) -> int:
        return sum(f.status == 'analyzed' for f in self.files)

    @computed_field
    @property
    def skipped_files(self) -> int:
        return sum(f.status == 'skipped' for f in self.files)

    @computed_field
    @property
    def partial_files(self) -> int:
        return sum(f.status == 'partial' for f in self.files)

class SnippetRequest(BaseModel):
    code: str = Field(min_length=1, max_length=100_000)
    language: Language
    filename: str = Field(default='snippet', max_length=200)

class ContextRequest(BaseModel):
    project_id: str
    scope: Literal['project', 'file', 'block'] = 'file'
    file_id: str | None = None
    path: str | None = None  # Transitional path-based requests remain supported.
    difficulty: Literal['beginner', 'intermediate', 'experienced'] = 'beginner'
    start_line: int | None = Field(default=None, ge=1)
    end_line: int | None = Field(default=None, ge=1)

    @model_validator(mode='after')
    def validate_scope(self):
        if self.scope == 'project':
            if any(v is not None for v in (self.path, self.file_id, self.start_line, self.end_line)):
                raise ValueError('Project scope does not accept a file or line range.')
        elif not (self.file_id or self.path):
            raise ValueError('Provide file_id for file or block scope.')
        if self.scope == 'block' and (self.start_line is None or self.end_line is None):
            raise ValueError('Block scope requires start_line and end_line.')
        if self.start_line and self.end_line and self.start_line > self.end_line:
            raise ValueError('start_line must not exceed end_line.')
        return self

class ContextChunk(BaseModel):
    file_id: str = ''
    path: str
    start_line: int
    end_line: int
    code: str

class ExplanationContext(BaseModel):
    project_id: str
    scope: Literal['project', 'file', 'block'] = 'file'
    difficulty: Literal['beginner', 'intermediate', 'experienced'] = 'beginner'
    language: Language | None = None
    selected: ContextChunk | None = None
    related: list[ContextChunk]
    constructs: list[Construct]
    concepts: list[str]
    relationships: list[Relationship]
    truncated: bool
    limitations: list[str]
    metadata: dict = Field(default_factory=dict)

class ExplanationSection(BaseModel):
    file_id: str | None = None
    title: str
    explanation: str
    start_line: int = Field(ge=1)
    end_line: int = Field(ge=1)

class Explanation(BaseModel):
    overview: str = Field(min_length=1, max_length=6000)
    sections: list[ExplanationSection] = Field(max_length=20)
    concepts: list[str] = Field(default_factory=list, max_length=40)
    limitations: list[str] = Field(default_factory=list)

    @computed_field
    @property
    def summary(self) -> str:
        return self.overview

    @computed_field
    @property
    def source_references(self) -> list[dict]:
        return [{'file_id': s.file_id, 'start_line': s.start_line, 'end_line': s.end_line} for s in self.sections]
