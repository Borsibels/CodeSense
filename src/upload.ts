export type InputMode = 'zip' | 'files' | 'folder' | 'paste';
export type ProjectInput = { mode: 'zip' | 'files' | 'folder'; files: File[] } | { mode: 'paste'; code: string; language: 'python' | 'javascript' | 'html' | 'css'; filename: string };
const EXCLUDED = new Set(['node_modules', '.git', '.venv', 'venv', '__pycache__', 'dist', 'build', '.next']);
export function folderFiles(files: File[]): File[] {
  return files.filter(file => {
    const parts = (file.webkitRelativePath || file.name).split('/');
    return !parts.some(part => EXCLUDED.has(part)) && !file.name.toLowerCase().startsWith('.env');
  });
}
export function sourceUploadError(files: readonly Pick<File, 'name' | 'size'>[], folder = false): string | null {
  if (!files.length) return 'Choose at least one source file.';
  if (files.length > 1000) return 'Choose at most 1000 files.';
  if (files.reduce((total, file) => total + file.size, 0) > 10 * 1024 * 1024) return 'Files exceed the 10 MB combined upload limit.';
  if (!folder && files.some(file => !/\.(py|js|html|htm|css)$/i.test(file.name))) return 'Choose Python, JavaScript, HTML or CSS source files.';
  if (!folder && files.some(file => file.size > 100 * 1024)) return 'Each source file must be at most 100 KB.';
  return null;
}
export function uploadError(files: readonly Pick<File, "name" | "size">[]): string | null {
  if (files.length !== 1) return "Choose one ZIP archive at a time.";
  if (!files[0].name.toLowerCase().endsWith(".zip")) return "Choose a .zip archive containing your source files.";
  if (files[0].size === 0) return "This archive is empty. Choose another ZIP file.";
  if (files[0].size > 10 * 1024 * 1024) return "The ZIP exceeds the 10 MB upload limit.";
  return null;
}
