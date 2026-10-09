import type { Language } from './types';

const extensions: Record<string, Language> = { py: 'python', js: 'javascript', html: 'html', htm: 'html', css: 'css' };
export const sourceExtension: Record<Language, string> = { python: 'py', javascript: 'js', html: 'html', css: 'css' };
export function detectLanguage(code: string, filename = ''): Language | null {
  const extension = filename.trim().split('.').pop()?.toLowerCase();
  if (filename.trim().includes('.')) return extensions[extension || ''] || null;
  const text = code.trim();
  if (!text) return null;
  if (/^(?:<!doctype\s+html|<\/?[a-z][\w:-]*(?:\s[^<>]*|\s*)\/?>)/i.test(text)) return 'html';
  const python = /(?:^|\n)\s*(?:async\s+def|def|class)\s+\w+[^\n]*:\s*(?:#.*)?(?:\n|$)|(?:^|\n)\s*(?:from\s+[\w.]+\s+import|import\s+[\w.]+\s*$)|(?:^|\n)\s*(?:if|elif|for|while|with|try|except)[^\n]*:\s*(?:#.*)?(?:\n|$)|\b(?:print|range)\s*\(|\b(?:True|False|None)\b/m.test(text);
  const javascript = /\b(?:const|let|var|function)\s+\w+|=>|\bconsole\s*\.|\b(?:document|window)\s*\.|\b(?:export|import)\s+(?:default|\{|\*|[\w]+\s+from)|(?:^|\n)\s*(?:if|for|while)\s*\(/m.test(text);
  if (javascript && !python) return 'javascript';
  if (/(?:^|\n)\s*@(?:media|supports|import|charset|keyframes)\b/i.test(text) || /[^{}]+\{\s*(?:\/\*[\s\S]*?\*\/\s*)?(?:--[\w-]+|[a-z][\w-]*)\s*:\s*[^{}]+[;}]/i.test(text)) return 'css';
  if (python === javascript) return null;
  return python ? 'python' : 'javascript';
}
