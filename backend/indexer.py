from pathlib import PurePosixPath
from .models import FileTreeNode, Project

def enrich_project(project: Project) -> Project:
    """Stable session IDs and deterministic metadata shared by frontend and AI."""
    roots = []
    by_path = {}
    for file in sorted(project.files, key=lambda f: f.path):
        import uuid
        file.file_id = str(uuid.uuid5(uuid.UUID(project.id), file.path))
        if file.status != 'skipped':
            project.language_counts[file.language] = project.language_counts.get(file.language, 0) + 1
            if PurePosixPath(file.path).name.lower() in ('index.html', 'main.py', 'app.py', '__main__.py', 'main.js', 'app.js', 'index.js'):
                project.entry_points.append(file.file_id)
        if file.reason:
            project.warnings.append(f'{file.path}: {file.reason}')
        parent = roots
        accumulated = []
        parts = PurePosixPath(file.path).parts
        for part in parts[:-1]:
            accumulated.append(part)
            path = '/'.join(accumulated)
            if path not in by_path:
                node = FileTreeNode(name=part, path=path, type='directory')
                by_path[path] = node
                parent.append(node)
            parent = by_path[path].children
        parent.append(FileTreeNode(name=parts[-1], path=file.path, type='file', file_id=file.file_id, status=file.status))
    project.file_tree = roots
    ids = {f.path: f.file_id for f in project.files}
    for relation in project.relationships:
        relation.source_id = ids.get(relation.source)
        relation.target_id = ids.get(relation.target) if relation.resolved else None
    return project
