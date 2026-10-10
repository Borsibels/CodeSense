import type { Difficulty, Language } from './types';
import type { ProjectInput } from './upload';

export interface SourceFile { file_id: string; path: string; language: Language | null; status: 'analyzed' | 'partial' | 'skipped'; reason: string | null; concepts: string[] }
export interface TreeNode { name: string; path: string; type: 'directory' | 'file'; file_id: string | null; status: string | null; children: TreeNode[] }
export interface Relationship { source: string; target: string; source_id: string; target_id: string | null; kind: string; resolved: boolean }
export interface Project { project_id: string; name: string; files: SourceFile[]; file_tree: TreeNode[]; relationships: Relationship[]; warnings: string[]; analyzed_files: number; skipped_files: number; partial_files: number }
export interface Health { backend: string; ai: { status: string; model: string | null }; analysis?: { available: boolean; reason: string | null }; storage: string; languages: Language[] }
export type Scope = 'project' | 'file' | 'block';

// Project analysis: POST /api/projects/{id}/analysis. Fields say in the backend contract whether they are
// AI-written or deterministic (computed by Sift from the real project).
export type Depth = 'beginner' | 'intermediate' | 'advanced';
export type Intent = 'overview' | 'explain' | 'debug';
export interface LineSpan { start_line: number; end_line: number }
export interface SelectionInfo { scope: 'project' | 'file' | 'symbol'; requested: LineSpan | null; analyzed_symbol: string | null; analyzed_lines: LineSpan | null; expanded: boolean; note: string | null }
export type LocationStatus = 'in_context' | 'outline_only' | 'file_only' | 'none' | 'rejected';
export interface AnalysisStep { title: string; description: string; file_path: string | null; start_line: number | null; end_line: number | null; location_status: LocationStatus; location_issue: string | null }
export type FindingVerification = 'source_verified' | 'hypothesis' | 'unsupported';
export type Tier = 'possible_problem' | 'worth_checking';
export interface Evidence { source_excerpt: string; excerpt_start_line: number; excerpt_end_line: number; excerpt_matched: boolean }
export interface Finding { id: string; title: string; category: string; problem: string; what_could_happen: string; likely_cause: string; suggestion: string; severity: string; confidence: string; verification: FindingVerification; file_path: string | null; start_line: number | null; end_line: number | null; evidence: Evidence | null; evidence_issue: string | null; tier: Tier; tier_reasons: string[] }
export interface PatternCheck { id: string; rule: string; strength: 'problem_if_assumptions_hold' | 'worth_checking'; title: string; explanation: string; assumptions: string[]; parser: 'confirmed' | 'heuristic'; file_path: string; start_line: number; end_line: number; source_excerpt: string; excerpt_start_line: number; shown_to_ai: boolean; corroborates: string[] }
export interface AnalysisRelationship { from: string; to: string; kind: 'uses' | 'loads' | 'entry_point'; resolved: boolean }
export interface Coverage { files_total: number; full_files: string[]; partial_files: { path: string }[]; outline_only_files: string[]; not_included_total: number; excluded_total: number }
export interface Analysis {
  status: 'completed'; notice: string; intent: Intent; depth: Depth; target_file: string | null; target_symbols: string[];
  summary: string; debug_outcome: 'no_clear_problem' | 'possible_problems' | null; pattern_checks: PatternCheck[]; relationships: AnalysisRelationship[];
  analogy: string | null; explanations: AnalysisStep[]; role_in_app: string | null; concept_to_learn: { name: string; explanation: string } | null;
  findings: Finding[]; assumptions: string[]; glossary: { term: string; meaning: string }[]; limitations: string[]; coverage: Coverage;
  generation: { model: string; attempts: number; mode: 'standard' | 'compact' }; timings_ms: Record<string, number>;
  project_id: string; file_id: string | null; selection: SelectionInfo;
}
export interface AnalysisBody { intent: Intent; depth: Depth; file_id?: string; start_line?: number; end_line?: number }
// The UI's "experienced" is the backend's "advanced".
export const DEPTH: Record<Difficulty, Depth> = { beginner: 'beginner', intermediate: 'intermediate', experienced: 'advanced' };
// Up to three local model calls; longer than the default request timeout.
export const ANALYSIS_TIMEOUT_MS = 330000;
export interface Exercise { id: string; title: string; objective: string; language: Language; difficulty: Difficulty; concepts: string[]; starter_code: string; hint_count: number; verification_policy: string; origin?: 'ai_missing_line'; source_path?: string; missing_line?: number }
export interface ChallengeSelection { status: 'selected' | 'no_match'; challenge: Exercise | null; match?: { kind: 'concept' | 'general' | 'source'; concepts: string[] }; reason?: string }
export interface Verification { correct: boolean; feedback: string; limitations: string[] }

export class ApiError extends Error {
  status: number;
  code?: string;
  constructor(message: string, status: number, code?: string) { super(message); this.status = status; this.code = code; }
}
export async function request<T>(path: string, options: RequestInit = {}, timeoutMs = 100000): Promise<T> {
  const controller = new AbortController();
  const timeout = window.setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetch(`/api${path}`, { ...options, signal: controller.signal });
    const data = response.status === 204 ? null : await response.json().catch(() => null);
    if (!response.ok) {
      // Two envelopes exist: the session host's {code, detail} and the analysis engine's {error: {code, message}}.
      const detail = typeof data?.detail === 'string' ? data.detail : typeof data?.error?.message === 'string' ? data.error.message : `Request failed (${response.status}).`;
      throw new ApiError(detail, response.status, data?.code ?? data?.error?.code);
    }
    if (response.status !== 204 && data === null) throw new ApiError('The server returned an invalid response.', 502);
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) throw new ApiError('The request timed out. Try again with a smaller selection.', 504);
    throw new ApiError('Cannot reach the local backend. Start it on port 8000 and retry.', 0);
  } finally { window.clearTimeout(timeout); }
}
export function post<T>(path: string, body: unknown, timeoutMs?: number): Promise<T> {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }, timeoutMs);
}
export function analysisBody(intent: Intent, difficulty: Difficulty, file?: { file_id: string } | null, range?: LineSpan | null): AnalysisBody {
  return { intent, depth: DEPTH[difficulty], ...(file && intent !== 'overview' ? { file_id: file.file_id } : {}), ...(range && intent !== 'overview' ? { start_line: range.start_line, end_line: range.end_line } : {}) };
}
export function analyzeProject(projectId: string, body: AnalysisBody): Promise<Analysis> {
  return post<Analysis>(`/projects/${encodeURIComponent(projectId)}/analysis`, body, ANALYSIS_TIMEOUT_MS);
}
export function uploadProject(file: File): Promise<Project> {
  const data = new FormData(); data.append('file', file);
  return request<Project>('/projects/upload', { method: 'POST', body: data });
}
export function ingestProject(input: ProjectInput): Promise<Project> {
  if (input.mode === 'paste') return post<Project>('/projects/snippet', { code: input.code, language: input.language, filename: input.filename });
  if (input.mode === 'zip') return uploadProject(input.files[0]);
  const data = new FormData();
  input.files.forEach(file => data.append('files', file));
  const paths = input.files.map(file => input.mode === 'folder' ? file.webkitRelativePath || file.name : file.name);
  data.append('paths', JSON.stringify(paths));
  data.append('name', input.mode === 'folder' ? paths[0].split('/')[0] : input.files.length === 1 ? input.files[0].name : 'Uploaded files');
  return request<Project>('/projects/files', { method: 'POST', body: data });
}
