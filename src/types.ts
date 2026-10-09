export type Difficulty = "beginner" | "intermediate" | "experienced";
export type Language = "html" | "css" | "javascript" | "python";
export type FileStatus = "analyzed" | "partial" | "skipped" | "unsupported";
export type ViewState = "empty" | "loading" | "error";
export interface FileNode { path: string; language?: Language; status: FileStatus; children?: FileNode[] }
export interface Relationship { from: string; to: string; kind: string; resolved: boolean }
export interface ExplanationData { level: "project" | "file" | "selection"; summary: string; concepts: string[]; steps: { lines: [number, number]; text: string }[]; relationships: Relationship[]; limits: string[] }
export interface Challenge { id: string; language: Language; difficulty: Difficulty; concept_tags: string[]; instructions: string; starter_code: string; hint_levels: string[]; is_general: boolean }
export interface SubmitResult { criteria: { label: string; met: boolean }[]; overall: "passed" | "partial" | "not_met" }
export const LIMITS = { files: 30, totalMB: 2, perFileKB: 100 }; // from config; will come from backend later
