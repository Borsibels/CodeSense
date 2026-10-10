// Sizing rules for the three-panel workspace. Pure functions, so they can be tested without a DOM.
// Panel sizes are stored as fractions of the shared width, so the proportions survive window resizes.
export interface Layout { explorer: number; assistant: number; collapsed: boolean }
export type DividerId = 'explorer' | 'assistant';

export const MIN_PX = { explorer: 170, editor: 340, assistant: 320 } as const;
export const DIVIDER_PX = 6;
export const RAIL_PX = 40;
/** Below this width the panels become a Files / Code / Ask Snip switcher. */
export const COMPACT_QUERY = '(max-width: 1179px)';
/** Storage key, not branding: it keeps the old product name so panel sizes people already saved still load after the rename to Sift. */
export const LAYOUT_KEY = 'codesense.workspace.layout.v1';
export const DEFAULT_LAYOUT: Layout = { explorer: 0.17, assistant: 0.35, collapsed: false };

const clamp = (n: number, lo: number, hi: number) => Math.min(Math.max(n, lo), Math.max(lo, hi));

/** Width the panels share once the dividers (and the collapsed explorer rail) are taken out. */
export const sharedWidth = (total: number, collapsed: boolean) => Math.max(0, total - (collapsed ? RAIL_PX + DIVIDER_PX : DIVIDER_PX * 2));

/** The pixel width each panel gets at this container width. */
export function panelWidths(layout: Layout, total: number) {
  const shared = sharedWidth(total, layout.collapsed);
  const explorer = layout.collapsed ? RAIL_PX : layout.explorer * shared;
  // Collapsing keeps the explorer's remembered share out of the way; the other two panels split the whole width.
  const assistant = (layout.collapsed ? layout.assistant / (1 - layout.explorer) : layout.assistant) * shared;
  const editor = total - (layout.collapsed ? DIVIDER_PX : DIVIDER_PX * 2) - explorer - assistant;
  return { explorer, editor, assistant };
}

/** Sets one or both side panels to a pixel width, keeping every panel above its minimum. */
export function withWidths(layout: Layout, total: number, next: { explorer?: number; assistant?: number }): Layout {
  const shared = sharedWidth(total, layout.collapsed);
  if (shared <= 0) return layout;
  const now = panelWidths(layout, total);
  const explorer = layout.collapsed ? 0 : clamp(next.explorer ?? now.explorer, MIN_PX.explorer, shared - MIN_PX.editor - MIN_PX.assistant);
  const assistant = clamp(next.assistant ?? now.assistant, MIN_PX.assistant, shared - MIN_PX.editor - explorer);
  return layout.collapsed
    ? { ...layout, assistant: (assistant / shared) * (1 - layout.explorer) }
    : { ...layout, explorer: explorer / shared, assistant: assistant / shared };
}

/** A divider dragged to pointer position `x` inside a container that starts at `left`. */
export function dragDivider(layout: Layout, id: DividerId, x: number, left: number, total: number): Layout {
  return id === 'explorer'
    ? withWidths(layout, total, { explorer: x - left - DIVIDER_PX / 2 })
    : withWidths(layout, total, { assistant: left + total - x - DIVIDER_PX / 2 });
}

/** Keyboard resizing: a positive delta moves the divider to the right. */
export function nudgeDivider(layout: Layout, id: DividerId, deltaPx: number, total: number): Layout {
  const now = panelWidths(layout, total);
  return id === 'explorer' ? withWidths(layout, total, { explorer: now.explorer + deltaPx }) : withWidths(layout, total, { assistant: now.assistant - deltaPx });
}

/** `grid-template-columns` for explorer | divider | editor | divider | assistant. */
export function columnTemplate(layout: Layout): string {
  const { explorer: e, assistant: a } = layout;
  const weight = (n: number) => Math.max(1, Math.round(n * 1000));
  if (layout.collapsed) return `${RAIL_PX}px 0px minmax(${MIN_PX.editor}px, ${weight((1 - e - a) / (1 - e))}fr) ${DIVIDER_PX}px minmax(${MIN_PX.assistant}px, ${weight(a / (1 - e))}fr)`;
  return `minmax(${MIN_PX.explorer}px, ${weight(e)}fr) ${DIVIDER_PX}px minmax(${MIN_PX.editor}px, ${weight(1 - e - a)}fr) ${DIVIDER_PX}px minmax(${MIN_PX.assistant}px, ${weight(a)}fr)`;
}

/** Reads a saved layout, falling back to the defaults for anything missing or out of range. */
export function parseLayout(raw: string | null): Layout {
  try {
    const v = JSON.parse(raw ?? 'null');
    const ok = (n: unknown, lo: number, hi: number) => typeof n === 'number' && Number.isFinite(n) && n >= lo && n <= hi;
    if (v && ok(v.explorer, 0.08, 0.4) && ok(v.assistant, 0.15, 0.55) && v.explorer + v.assistant <= 0.8 && typeof v.collapsed === 'boolean') return { explorer: v.explorer, assistant: v.assistant, collapsed: v.collapsed };
  } catch { /* unreadable: use the defaults */ }
  return DEFAULT_LAYOUT;
}
