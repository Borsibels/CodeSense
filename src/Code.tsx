import { useEffect, useRef } from "react";
import { EditorView, basicSetup } from "codemirror";
import { python } from "@codemirror/lang-python"; import { javascript } from "@codemirror/lang-javascript";
import { html } from "@codemirror/lang-html"; import { css } from "@codemirror/lang-css";
import { oneDark } from "@codemirror/theme-one-dark";
import type { Language } from "./types";
const langs = { python, javascript, html, css };
const theme = EditorView.theme({ "&": { backgroundColor: "var(--code-bg)", color: "var(--text)", height: "100%" }, ".cm-gutters": { backgroundColor: "var(--code-bg)", color: "var(--muted)", borderRight: "2px solid var(--border)" }, ".cm-content,.cm-gutters": { fontFamily: "'JetBrains Mono',monospace", fontSize: "13px", lineHeight: "24px" }, ".cm-activeLine,.cm-activeLineGutter": { backgroundColor: "transparent" }, ".cm-cursor": { borderLeftColor: "var(--accent)" } }, { dark: true });
export function CodeEditor({ value = "", language = "python", readOnly = false, onChange, onSelection, onCursor, highlight }: { value?: string; language?: Language; readOnly?: boolean; onChange?: (v: string) => void; onSelection?: (range: { start_line: number; end_line: number } | null) => void; onCursor?: (caret: { line: number; col: number }) => void; highlight?: [number, number] | null }) {
  const host = useRef<HTMLDivElement>(null);
  const editor = useRef<EditorView>();
  const callbacks = useRef({ onChange, onSelection, onCursor }); callbacks.current = { onChange, onSelection, onCursor };
  const initial = useRef(value); initial.current = value;
  useEffect(() => {
    const v = new EditorView({ parent: host.current!, doc: initial.current, extensions: [basicSetup, langs[language](), oneDark, theme, EditorView.editable.of(!readOnly), EditorView.updateListener.of(u => {
      if (u.docChanged) callbacks.current.onChange?.(u.state.doc.toString());
      if (u.selectionSet || u.docChanged) {
        const selection = u.state.selection.main;
        const caret = u.state.doc.lineAt(selection.head);
        callbacks.current.onCursor?.({ line: caret.number, col: selection.head - caret.from + 1 });
        callbacks.current.onSelection?.(selection.empty ? null : { start_line: u.state.doc.lineAt(selection.from).number, end_line: u.state.doc.lineAt(Math.max(selection.from, selection.to - 1)).number });
      }
    })] });
    editor.current = v;
    return () => v.destroy();
  }, [language, readOnly]);
  useEffect(() => { const v = editor.current; if (v && v.state.doc.toString() !== value) v.dispatch({ changes: { from: 0, to: v.state.doc.length, insert: value } }); }, [value]);
  useEffect(() => { const v = editor.current; if (v && highlight && highlight[0] >= 1 && highlight[1] <= v.state.doc.lines) { const anchor = v.state.doc.line(highlight[0]).from; v.dispatch({ selection: { anchor, head: v.state.doc.line(highlight[1]).to }, effects: EditorView.scrollIntoView(anchor, { y: 'center' }) }); } }, [highlight, value]);
  return <div className="cm-host" ref={host} aria-label="Code" />;
}
