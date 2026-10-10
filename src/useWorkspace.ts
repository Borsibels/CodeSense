import { useEffect, useRef, useState } from 'react';
import { post, request, ingestProject, analyzeProject, analysisBody } from './api';
import type { ProjectInput } from './upload';
import type { Analysis, ChallengeSelection, Exercise, Health, Project, Scope, SourceFile, Verification } from './api';
import type { Difficulty, Language } from './types';

type Operation = 'upload' | 'source' | 'analysis' | 'problems' | 'challenge' | 'submit' | 'hint' | 'solution';
export function useWorkspace() {
  const [project, setProject] = useState<Project | null>(null);
  const [selected, setSelected] = useState<SourceFile | null>(null);
  const [code, setCode] = useState('');
  const [range, setRange] = useState<{ start_line: number; end_line: number } | null>(null);
  const [scope, setScope] = useState<Scope>('file');
  const [difficulty, setDifficulty] = useState<Difficulty>('beginner');
  const [explanation, setExplanation] = useState<Analysis | null>(null);
  const [problems, setProblems] = useState<Analysis | null>(null);
  const [health, setHealth] = useState<Health | null>(null);
  const [healthError, setHealthError] = useState('');
  const [connectionNotice, setConnectionNotice] = useState('');
  const [checking, setChecking] = useState(false);
  const [exercise, setExercise] = useState<Exercise | null>(null);
  const [general, setGeneral] = useState(false);
  const [answer, setAnswer] = useState('');
  const [hints, setHints] = useState<string[]>([]);
  const [solution, setSolution] = useState('');
  const [result, setResult] = useState<Verification | null>(null);
  const [challengeDifficulty, setChallengeDifficulty] = useState<Difficulty>('beginner');
  const [challengeLanguage, setChallengeLanguage] = useState<Language>('python');
  const [busy, setBusy] = useState<Partial<Record<Operation, boolean>>>({});
  const [errors, setErrors] = useState<Partial<Record<Operation, string>>>({});
  const sourceEpoch = useRef(0), explanationEpoch = useRef(0), problemsEpoch = useRef(0), exerciseEpoch = useRef(0);
  const mark = (op: Operation, value: boolean) => setBusy(s => ({ ...s, [op]: value }));
  const fail = (op: Operation, error: unknown = '') => setErrors(s => ({ ...s, [op]: error instanceof Error ? error.message : String(error) }));

  async function checkHealth(showNotice = false) {
    setChecking(true);
    try {
      const data = await request<Health>('/health', {}, 5000);
      setHealth(data); setHealthError('');
      if (showNotice) {
        setConnectionNotice(data.ai.status === 'ready'
          ? 'Local AI is active and ready.'
          : data.ai.status === 'model_missing'
            ? `Ollama is running, but ${data.ai.model || 'the Qwen model'} is not downloaded yet. Follow the setup steps and run the model download command.`
            : 'Local AI is not active yet. Install and open Ollama, then download the model using the setup steps.');
      }
      return data;
    }
    catch (e) {
      setHealth(null); setHealthError((e as Error).message);
      if (showNotice) setConnectionNotice('CodeSense could not check the AI status. Make sure the backend is running, then try again.');
      return null;
    }
    finally { setChecking(false); }
  }
  useEffect(() => { void checkHealth(); const timer = window.setInterval(checkHealth, 15000); return () => window.clearInterval(timer); }, []);

  async function selectFile(file: SourceFile, active = project, preserveExplanation = false) {
    if (!active) return;
    const epoch = ++sourceEpoch.current; ++explanationEpoch.current; ++problemsEpoch.current;
    setSelected(file); setCode(''); setRange(null); if (!preserveExplanation) { setExplanation(null); setProblems(null); setScope('file'); }
    fail('source'); fail('analysis'); mark('analysis', false); fail('problems'); mark('problems', false);
    if (file.status === 'skipped') { mark('source', false); return; }
    mark('source', true);
    try {
      const data = await request<{ code: string }>(`/projects/${active.project_id}/files/${file.file_id}`);
      if (epoch === sourceEpoch.current) setCode(data.code);
    } catch (e) { if (epoch === sourceEpoch.current) fail('source', e); }
    finally { if (epoch === sourceEpoch.current) mark('source', false); }
  }
  async function upload(input: ProjectInput, level: Difficulty) {
    mark('upload', true); fail('upload');
    try {
      const data = await ingestProject(input);
      ++sourceEpoch.current; ++explanationEpoch.current; ++problemsEpoch.current; ++exerciseEpoch.current;
      setProject(data); setDifficulty(level); setChallengeDifficulty(level);
      setSelected(null); setCode(''); setExplanation(null); setProblems(null); setRange(null);
      setExercise(null); setAnswer(''); setHints([]); setSolution(''); setResult(null);
      setErrors({});
      setBusy({ upload: true });
      const first = data.files.find(f => f.status !== 'skipped');
      if (first) await selectFile(first, data);
      return true;
    } catch (e) { fail('upload', e); return false; }
    finally { mark('upload', false); }
  }
  function changeScope(value: Scope) { ++explanationEpoch.current; setScope(value); setExplanation(null); fail('analysis'); mark('analysis', false); }
  function changeDifficulty(value: Difficulty) { ++explanationEpoch.current; ++problemsEpoch.current; setDifficulty(value); setExplanation(null); setProblems(null); fail('analysis'); mark('analysis', false); fail('problems'); mark('problems', false); }
  async function explain(value = scope) {
    if (!project) return;
    if (value !== 'project' && (!selected || selected.status === 'skipped')) return;
    if (value === 'block' && !range) return;
    const epoch = ++explanationEpoch.current;
    setScope(value); mark('analysis', true); fail('analysis'); setExplanation(null);
    try {
      const body = analysisBody(value === 'project' ? 'overview' : 'explain', difficulty, value === 'project' ? null : selected, value === 'block' ? range : null);
      const data = await analyzeProject(project.project_id, body);
      if (epoch === explanationEpoch.current) setExplanation(data);
    } catch (e) { if (epoch === explanationEpoch.current) fail('analysis', e); }
    finally { if (epoch === explanationEpoch.current) mark('analysis', false); }
  }
  // Looks for possible problems in the selected lines if there are any, otherwise in the whole file.
  async function checkProblems() {
    if (!project || !selected || selected.status === 'skipped') return;
    const epoch = ++problemsEpoch.current;
    mark('problems', true); fail('problems'); setProblems(null);
    try {
      const data = await analyzeProject(project.project_id, analysisBody('debug', difficulty, selected, range));
      if (epoch === problemsEpoch.current) setProblems(data);
    } catch (e) { if (epoch === problemsEpoch.current) fail('problems', e); }
    finally { if (epoch === problemsEpoch.current) mark('problems', false); }
  }
  async function loadChallenge(level = challengeDifficulty, language = challengeLanguage) {
    if (!project) { fail('challenge', 'Add source code first so the exercise language can be detected.'); return; }
    const epoch = ++exerciseEpoch.current;
    setChallengeDifficulty(level); setChallengeLanguage(language); mark('challenge', true);
    setExercise(null); setHints([]); setSolution(''); setResult(null); setAnswer('');
    (['challenge', 'hint', 'solution', 'submit'] as Operation[]).forEach(op => fail(op));
    try {
      const body = project ? { project_id: project.project_id, difficulty: level,
        ...(selected && selected.status !== 'skipped' ? { file_id: selected.file_id } : {}) }
        : { language, difficulty: level };
      const data = await post<ChallengeSelection>('/challenges/generate', body);
      if (epoch !== exerciseEpoch.current) return;
      if (!data.challenge) throw new Error(data.reason || 'No verified exercise matches this request.');
      setExercise(data.challenge); setAnswer(data.challenge.starter_code); setChallengeLanguage(data.challenge.language);
      if (difficulty !== level) { setDifficulty(level); ++explanationEpoch.current; ++problemsEpoch.current; setExplanation(null); setProblems(null); }
      setGeneral(data.match?.kind === 'general');
    } catch (e) { if (epoch === exerciseEpoch.current) fail('challenge', e); }
    finally { if (epoch === exerciseEpoch.current) mark('challenge', false); }
  }
  async function submit() {
    if (!exercise) return false;
    const epoch = exerciseEpoch.current;
    mark('submit', true); fail('submit'); setResult(null);
    try { const data = await post<Verification>('/challenges/submit', { challenge_id: exercise.id, code: answer }); if (epoch === exerciseEpoch.current) { setResult(data); return true; } return false; }
    catch (e) { if (epoch === exerciseEpoch.current) fail('submit', e); return false; }
    finally { mark('submit', false); }
  }
  async function revealHint() {
    if (!exercise || hints.length >= exercise.hint_count) return;
    const epoch = exerciseEpoch.current;
    mark('hint', true); fail('hint');
    try { const data = await request<{ hints: string[] }>(`/challenges/${exercise.id}/hints/${hints.length + 1}`); if (epoch === exerciseEpoch.current) setHints(data.hints); }
    catch (e) { if (epoch === exerciseEpoch.current) fail('hint', e); }
    finally { mark('hint', false); }
  }
  async function revealSolution() {
    if (!exercise) return;
    const epoch = exerciseEpoch.current;
    mark('solution', true); fail('solution');
    try { const data = await request<{ solution: string }>(`/challenges/${exercise.id}/solution`); if (epoch === exerciseEpoch.current) setSolution(data.solution); }
    catch (e) { if (epoch === exerciseEpoch.current) fail('solution', e); }
    finally { mark('solution', false); }
  }
  function editAnswer(value: string) { setAnswer(value); setResult(null); }
  return { project, selected, code, range, setRange, scope, changeScope, difficulty, changeDifficulty, explanation, problems,
    health, healthError, checking, checkHealth, connectionNotice, dismissConnectionNotice: () => setConnectionNotice(''), exercise, general, answer, editAnswer, hints, solution, result,
    challengeDifficulty, challengeLanguage, busy, errors, upload, selectFile, explain, checkProblems, loadChallenge, submit, revealHint, revealSolution };
}
export type Workspace = ReturnType<typeof useWorkspace>;
