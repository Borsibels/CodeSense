import type { Scope } from './api';
import type { Difficulty } from './types';

export type Reference = { path: string; start_line: number; end_line: number };
export type QuestionReply = { answer: string; limitations: string[]; references?: Reference[]; context_used?: Reference[] };
export type ConversationTurn = QuestionReply & { question: string; context: string; scopeKey: string };
export type ConversationScope = { project_id: string; scope: Scope; difficulty: Difficulty; file_id?: string; start_line?: number; end_line?: number };

export function conversationScope(projectId: string, scope: Scope, fileId: string | undefined,
  range: { start_line: number; end_line: number } | null, difficulty: Difficulty): ConversationScope {
  const actual = scope === 'block' && !range ? 'file' : scope;
  return { project_id: projectId, scope: actual, difficulty,
    ...(actual !== 'project' ? { file_id: fileId } : {}), ...(actual === 'block' && range ? range : {}) };
}
export function conversationKey(body: ConversationScope): string {
  return [body.project_id, body.scope, body.file_id || '', body.start_line || 0, body.end_line || 0, body.difficulty].join(':');
}
export function recentHistory(turns: ConversationTurn[], key: string) {
  return turns.filter(t => t.scopeKey === key).slice(-3).map(t => ({ question: t.question, answer: t.answer }));
}
