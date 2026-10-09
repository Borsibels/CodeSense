import type { Difficulty, Language } from './types';

export interface SourceFile { file_id: string; path: string; language: Language | null; status: 'analyzed' | 'partial' | 'skipped'; reason: string | null; concepts: string[] }
export interface TreeNode { name: string; path: string; type: 'directory' | 'file'; file_id: string | null; status: string | null; children: TreeNode[] }
export interface Relationship { source: string; target: string; source_id: string; target_id: string | null; kind: string; resolved: boolean }
export interface Project { project_id: string; name: string; files: SourceFile[]; file_tree: TreeNode[]; relationships: Relationship[]; warnings: string[]; analyzed_files: number; skipped_files: number; partial_files: number }
export interface Health { backend: string; ai: { status: string; model: string | null }; storage: string; languages: Language[] }
export type Scope = 'project' | 'file' | 'block';
export interface AnalysisRequest { project_id: string; scope: Scope; file_id?: string; start_line?: number; end_line?: number; difficulty: Difficulty }
export interface Explanation { summary: string; sections: { title: string; explanation: string; file_id: string; start_line: number; end_line: number }[]; concepts: string[]; limitations: string[] }
export interface Exercise { id: string; title: string; objective: string; language: Language; difficulty: Difficulty; concepts: string[]; starter_code: string; hint_count: number; verification_policy: string }
export interface ChallengeSelection { status: 'selected' | 'no_match'; challenge: Exercise | null; match?: { kind: 'concept' | 'general'; concepts: string[] }; reason?: string }
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
      const detail = typeof data?.detail === 'string' ? data.detail : `Request failed (${response.status}).`;
      throw new ApiError(detail, response.status, data?.code);
    }
    if (response.status !== 204 && data === null) throw new ApiError('The server returned an invalid response.', 502);
    return data as T;
  } catch (error) {
    if (error instanceof ApiError) throw error;
    if (controller.signal.aborted) throw new ApiError('The request timed out. Try again with a smaller selection.', 504);
    throw new ApiError('Cannot reach the local backend. Start it on port 8000 and retry.', 0);
  } finally { window.clearTimeout(timeout); }
}
export function post<T>(path: string, body: unknown): Promise<T> {
  return request<T>(path, { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
}
export function uploadProject(file: File): Promise<Project> {
  const data = new FormData(); data.append('file', file);
  return request<Project>('/projects/upload', { method: 'POST', body: data });
}
