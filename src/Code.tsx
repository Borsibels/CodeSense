import { useEffect, useRef } from "react";
import { EditorView, basicSetup } from "codemirror";
import { python } from "@codemirror/lang-python"; import { javascript } from "@codemirror/lang-javascript";
import { html } from "@codemirror/lang-html"; import { css } from "@codemirror/lang-css";
import { oneDark } from "@codemirror/theme-one-dark";
import type { Language } from "./types";
const langs = { python, javascript, html, css };
const theme = EditorView.theme({ "&": { backgroundColor: "var(--code-bg)", color: "var(--text)", height: "100%" }, ".cm-gutters": { backgroundColor: "var(--code-bg)", color: "var(--muted)", borderRight: "2px solid var(--border)" }, ".cm-content,.cm-gutters": { fontFamily: "'JetBrains Mono',monospace", fontSize: "13px", lineHeight: "24px" }, ".cm-activeLine,.cm-activeLineGutter": { backgroundColor: "transparent" }, ".cm-cursor": { borderLeftColor: "var(--accent)" } }, { dark: true });
export function CodeEditor({ value = "", language = "python", readOnly = false, onChange }: { value?: string; language?: Language; readOnly?: boolean; onChange?: (v: string) => void }) {
  const host = useRef<HTMLDivElement>(null);
  useEffect(() => {
    const v = new EditorView({ parent: host.current!, doc: value, extensions: [basicSetup, langs[language](), oneDark, theme, EditorView.editable.of(!readOnly), EditorView.updateListener.of(u => u.docChanged && onChange?.(u.state.doc.toString()))] });
    return () => v.destroy();
  }, [language, readOnly, value]);
  return <div className="cm-host" ref={host} aria-label="Code" />;
}
