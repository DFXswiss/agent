// The pairing in the fragment of `/connect#…` (11-agent-access.md section 5.1) is read once and removed from the
// address with `history.replaceState` (keeping the router's history state), like the invitation code
// (`screens/invite/fragment.ts`): it stays in neither history nor the address bar and is never sent to the hub. A
// second read for the same history entry (React renders twice in development) returns the same text.
export interface FragmentWindow {
  readonly location: { readonly hash: string; readonly pathname: string; readonly search: string };
  readonly history: Pick<History, 'replaceState' | 'state'>;
}

let taken: { readonly key: string; readonly text: string } | null = null;

function entryKey(state: unknown): string {
  return typeof state === 'object' && state !== null && 'key' in state ? String(state.key) : JSON.stringify(state);
}

/** The fragment without `#` (empty if none), removed from the address on first read. */
export function takeConnectFragment(win: FragmentWindow = window): string {
  const { hash, pathname, search } = win.location;
  if (hash.length > 1) {
    taken = { key: entryKey(win.history.state), text: hash.slice(1) };
    win.history.replaceState(win.history.state, '', pathname + search);
    return taken.text;
  }
  return taken !== null && taken.key === entryKey(win.history.state) ? taken.text : '';
}
