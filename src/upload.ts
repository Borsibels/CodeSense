export function uploadError(files: readonly Pick<File, "name" | "size">[]): string | null {
  if (files.length !== 1) return "Choose one ZIP archive at a time.";
  if (!files[0].name.toLowerCase().endsWith(".zip")) return "Choose a .zip archive containing your source files.";
  if (files[0].size === 0) return "This archive is empty. Choose another ZIP file.";
  return null;
}
