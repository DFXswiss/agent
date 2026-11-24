// U-WEB-list-markup (ADR-063 section 1): every list of the web client goes through the shared `WindowedList`
// (`src/ui/lists`). This source test finds list markup made by `.map(` – `<li>`/`<tr>` produced in a map callback, or
// a map inside `<ul>`/`<ol>`/`<tbody>` – anywhere else under `src/ui`. Fixed small lists are allowed by name with
// their number of places and the reason they stay small; a new unbounded list fails here.
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { WEB_DIR } from '../helpers';

interface Allowed {
  /** Places in the file (a new list in an allowed file fails as well). */
  readonly count: number;
  readonly reason: string;
}

/** Lists with a fixed small bound. */
const ALLOWED: Readonly<Record<string, Allowed>> = {
  'src/ui/responsive/BottomNav.tsx': { count: 2, reason: 'navigation entries of the registry (a handful)' },
  'src/ui/responsive/Workgroups.tsx': { count: 1, reason: 'workgroups of the org in the phone menu (navigation)' },
  'src/ui/shell/Nav.tsx': { count: 1, reason: 'navigation entries of the registry (a handful)' },
  'src/ui/shell/header/org-switcher.tsx': { count: 1, reason: 'organisations the user belongs to (a handful)' },
  'src/ui/composer/Composer.tsx': { count: 1, reason: 'chips of the composer (fixed suggestions)' },
  'src/ui/screens/security/SecurityScreen.tsx': { count: 1, reason: 'static points of the security page' },
  'src/ui/screens/errors/Unsupported.tsx': { count: 1, reason: 'the list of supported browsers (static)' },
  'src/ui/screens/wg-settings/WgSettingsScreen.tsx': {
    count: 3,
    reason: 'repositories, patterns and errors of one settings document (bounded by its schema)',
  },
  'src/ui/screens/invite/org/OrgInvite.tsx': { count: 1, reason: 'workgroups granted by one invitation' },
  'src/ui/settings/sections/passkeys/PasskeyList.tsx': { count: 1, reason: 'passkeys of one account (a handful)' },
  'src/ui/settings/sections/credentials/CredentialList.tsx': {
    count: 1,
    reason: 'one credential per supported kind (closed list)',
  },
  'src/ui/settings/sections/quotas/index.tsx': { count: 1, reason: 'quota windows of one provider (closed list)' },
  'src/ui/chat-items/operation/Card.tsx': { count: 1, reason: 'facts of one operation (counts, not its lists)' },
  'src/ui/countdown/CountdownPanel.tsx': { count: 1, reason: 'the fixed steps of the countdown' },
  'src/ui/shell/header/bell.tsx': { count: 1, reason: 'at most 3 toasts (core/store/slices/notifications.ts)' },
  'src/ui/shell/header/credentials.tsx': { count: 1, reason: 'at most 3 toasts (core/store/slices/credentials.ts)' },
  'src/ui/screens/session/children/ChildrenTab.tsx': {
    count: 2,
    reason: 'subagents of one session: at most 10 per task and depth 2 (hub operations/limits.py)',
  },
};

const SKIP = 'src/ui/lists/';

function sources(dir: string): string[] {
  return readdirSync(join(WEB_DIR, dir), { withFileTypes: true }).flatMap((entry) => {
    const path = `${dir}/${entry.name}`;
    if (entry.isDirectory()) return sources(path);
    return /\.tsx?$/.test(entry.name) ? [path] : [];
  });
}

const lineOf = (text: string, index: number): number => text.slice(0, index).split('\n').length;

/** How far after `.map(` a produced `<li>`/`<tr>` is looked for (a callback of a few lines). */
const REACH = 400;

/** Lines of list markup made by `.map(` in `text`. */
function listMarkup(text: string): number[] {
  const lines = new Set<number>();
  for (const map of text.matchAll(/\.map\(/g)) {
    const after = text.slice(map.index, map.index + REACH);
    const next = after.indexOf('.map(', 1);
    const item = /<(?:li|tr)[\s>]/.exec(next < 0 ? after : after.slice(0, next));
    if (item !== null) lines.add(lineOf(text, map.index));
  }
  for (const open of text.matchAll(/<(ul|ol|tbody)[\s>]/g)) {
    const close = text.indexOf(`</${open[1]}>`, open.index);
    const inner = text.slice(open.index, close < 0 ? undefined : close);
    const map = inner.indexOf('.map(');
    if (map >= 0) lines.add(lineOf(text, open.index + map));
  }
  return [...lines].sort((a, b) => a - b);
}

describe('list markup', () => {
  it('finds lists made by map', () => {
    expect(listMarkup('<ul>\n{rows.map((row) => (\n  <li key={row}>{row}</li>\n))}\n</ul>')).toEqual([2]);
    expect(listMarkup('rows.map((row) => <tr key={row} />)')).toEqual([1]);
    // a component per row inside a list
    expect(listMarkup('<ol className="x">\n  {rows.map((row) => <Row key={row} />)}\n</ol>')).toEqual([2]);
    expect(listMarkup('<tbody>{rows.map((row) => <Row key={row} />)}')).toEqual([1]);
    // not a list: maps outside list markup, an item made by a later map
    expect(listMarkup('<p>{names.map((name) => name).join(", ")}</p>')).toEqual([]);
    expect(listMarkup('a.map(f);\nb.map((x) => <li>{x}</li>)')).toEqual([2]);
    expect(listMarkup('<ul><li>static</li></ul>')).toEqual([]);
  });

  it('allows list markup only in the shared component and the named small lists', () => {
    const found = Object.fromEntries(
      sources('src/ui')
        .filter((path) => !path.startsWith(SKIP))
        .map((path) => [path, listMarkup(readFileSync(join(WEB_DIR, path), 'utf8')).length] as const)
        .filter(([, count]) => count > 0),
    );
    const allowed = Object.fromEntries(Object.entries(ALLOWED).map(([path, entry]) => [path, entry.count]));
    expect(found).toEqual(allowed);
  });

  it('scans the shared component as the place of every other list', () => {
    expect(listMarkup(readFileSync(join(WEB_DIR, 'src/ui/lists/WindowedList.tsx'), 'utf8'))).not.toEqual([]);
  });
});
