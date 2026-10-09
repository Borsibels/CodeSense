// Wording and small decisions for rendering an analysis. Pure functions, so they can be tested without a DOM.
import type { AnalysisStep, Finding, FindingVerification, PatternCheck, SelectionInfo, Tier } from './api';

export const rangeLabel = (start: number, end: number) => start === end ? `line ${start}` : `lines ${start}–${end}`;

export interface StepLocation { path: string; start: number | null; end: number | null; label: string }
/** A clickable source location for a step, or null. Only locations the backend verified are ever shown. */
export function stepLocation(step: Pick<AnalysisStep, 'file_path' | 'start_line' | 'end_line' | 'location_status'>): StepLocation | null {
  if (!step.file_path) return null;
  if (step.location_status === 'file_only') return { path: step.file_path, start: null, end: null, label: step.file_path };
  if (step.location_status !== 'in_context' && step.location_status !== 'outline_only') return null;
  if (step.start_line == null || step.end_line == null) return null;
  const range = rangeLabel(step.start_line, step.end_line);
  return { path: step.file_path, start: step.start_line, end: step.end_line, label: `${step.file_path} · ${range}${step.location_status === 'outline_only' ? ' (signature only)' : ''}` };
}

export const tierLabel = (tier: Tier) => tier === 'possible_problem' ? 'Possible problem' : 'Worth checking';

export const verificationLabel: Record<FindingVerification, { short: string; long: string }> = {
  source_verified: { short: 'Location verified', long: 'The cited file and lines exist, were shown to the AI, and the quoted code matches. This does not prove the problem is real.' },
  hypothesis: { short: 'Quote not matched', long: 'The cited lines exist, but the code the AI quoted is missing or does not match them.' },
  unsupported: { short: 'Location not verified', long: 'The location the AI gave could not be found in the code it was shown.' },
};

export const tierReasonText: Record<string, string> = {
  QUOTE_NOT_MATCHED: 'The code the AI quoted does not match the lines it points at.',
  LOCATION_NOT_VERIFIED: 'The location could not be verified.',
  IN_DEMO_CODE: 'The lines are example code that only runs when the file is run directly.',
  INPUT_ASSUMPTION: 'The claim is about input the code was never shown receiving.',
  CORROBORATED_BY_RULE: 'An independent automatic check flagged the same lines.',
};

export const outcomeLabel = (outcome: 'no_clear_problem' | 'possible_problems' | null) =>
  outcome === 'possible_problems' ? 'Possible problems found' : outcome === 'no_clear_problem' ? 'No clear problem found' : '';

export const strengthLabel = (strength: PatternCheck['strength']) => strength === 'problem_if_assumptions_hold' ? 'A problem if the assumptions hold' : 'Worth checking';

/** Findings split into the two tiers, keeping the backend's order. */
export function groupFindings(findings: Finding[]) {
  return { possible: findings.filter(f => f.tier === 'possible_problem'), worth: findings.filter(f => f.tier !== 'possible_problem') };
}

/** What was analysed, for the line under the result heading. */
export function scopeLabel(selection: SelectionInfo, targetFile: string | null): string {
  if (selection.scope === 'project') return targetFile ? `Project overview, focused on ${targetFile}` : 'Whole-project overview';
  const lines = selection.analyzed_lines ? ` (${rangeLabel(selection.analyzed_lines.start_line, selection.analyzed_lines.end_line)})` : '';
  if (selection.scope === 'symbol') return `${selection.analyzed_symbol ?? 'Selected code'}${lines} in ${targetFile ?? 'the selected file'}`;
  return `${targetFile ?? 'Selected file'}${lines}`;
}

/** A source excerpt with line numbers; lines inside the cited range are marked. */
export function numberedExcerpt(text: string, startLine: number, hit?: { start: number | null; end: number | null }) {
  return text.split('\n').map((line, i) => {
    const number = startLine + i;
    return { number, line, hit: !!hit && hit.start != null && hit.end != null && number >= hit.start && number <= hit.end };
  });
}
