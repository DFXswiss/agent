// The code in the fragment of `/invite#<code>` (06-web.md section 2.4) is read once and removed from the address
// with `history.replaceState` (keeping the router's history state), so it stays in neither history nor the address
// bar. It is never sent to the hub, logged or stored: only the screen's memory holds it. A second read for the same
// history entry (React renders twice in development) returns the same code.
export interface FragmentWindow {
  readonly location: { readonly hash: string; readonly pathname: string; readonly search: string };
  readonly history: Pick<History, 'replaceState' | 'state'>;
}

let taken: { readonly key: string; readonly code: string } | null = null;

/** The history entry: React Router's `key` if present, else the whole (structured-clone) state as JSON. */
function entryKey(state: unknown): string {
  return typeof state === 'object' && state !== null && 'key' in state ? String(state.key) : JSON.stringify(state);
}

/** The fragment without `#` (empty if none), removed from the address on first read. */
export function takeFragment(win: FragmentWindow = window): string {
  const { hash, pathname, search } = win.location;
  if (hash.length > 1) {
    taken = { key: entryKey(win.history.state), code: hash.slice(1) };
    win.history.replaceState(win.history.state, '', pathname + search);
    return taken.code;
  }
  return taken !== null && taken.key === entryKey(win.history.state) ? taken.code : '';
}

/** Forgets the remembered code (once the screen holds it in its state). */
export function forgetFragment(): void {
  taken = null;
}
