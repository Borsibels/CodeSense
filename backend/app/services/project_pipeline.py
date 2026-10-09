"""Deterministic project pipeline: ZIP bytes -> files -> static analysis -> dependency graph.

Pure and synchronous (CPU-bound on a bounded input); the API runs it in a worker
thread. It never touches the filesystem, the network or Ollama, and keeps nothing
after returning: the archive is closed and only derived data is returned.
"""

from __future__ import annotations

import logging

from app.config import ProjectSettings
from app.services.dependency_graph import build_graph
from app.services.project_discovery import discover_files
from app.services.project_ingestion import open_archive
from app.services.project_models import Deadline, ProjectAnalysis, ProjectStats
from app.services.static_analysis import analyze_files

logger = logging.getLogger(__name__)


def analyze_project(data: bytes, limits: ProjectSettings, deadline: Deadline | None = None) -> ProjectAnalysis:
    """Validate and analyse a project archive. Raises ``ArchiveError`` / ``ProcessingTimeout``."""
    deadline = deadline or Deadline(limits.processing_timeout)
    archive = open_archive(data, limits)
    try:
        discovery = discover_files(archive, limits, deadline)
        stats = ProjectStats(
            archive_bytes=archive.archive_bytes,
            entry_count=archive.entry_count,
            uncompressed_bytes=archive.uncompressed_bytes,
            source_bytes=sum(f.size_bytes for f in discovery.files),
        )
    finally:
        archive.close()

    analyses = analyze_files(discovery.files, deadline)
    graph = build_graph(discovery.files, analyses, discovery.excluded, deadline)
    # Counts only: never log file names or contents of an uploaded project.
    logger.info(
        "Analysed project: %d source files, %d excluded, %d edges",
        len(discovery.files),
        len(discovery.excluded),
        len(graph.edges),
    )
    return ProjectAnalysis(
        stats=stats,
        files=discovery.files,
        excluded=discovery.excluded,
        analyses=analyses,
        graph=graph,
    )
