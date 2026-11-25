// U-WEB-list-markup (ADR-086 section 1): every list of the web client goes through the shared `WindowedList` or the
// searchable `Picker` (`src/ui/lists`). This source test finds markup made by `.map(` – any keyed JSX element produced
// in a map callback (`<option key>`, `<label key>`, `<div key>`, `<Row key>` …), `<li>`/`<tr>` produced in one, or a
// map inside `<ul>`/`<ol>`/`<tbody>` – anywhere else under `src/ui`. Fixed small lists are allowed by name with their
// number of places and the reason they stay small; a new unbounded list or dialog choice fails here.
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
  'src/ui/shell/Header.tsx': { count: 1, reason: 'header items of the registry (a handful)' },
  'src/ui/shell/header/org-switcher.tsx': { count: 1, reason: 'organisations the user belongs to (a handful)' },
  'src/ui/subscriptions/SubscriptionManager.tsx': {
    count: 1,
    reason: 'subscriptions of the user (at most 40, ADR-090)',
  },
  'src/ui/subscriptions/UseEditor.tsx': { count: 1, reason: 'organisations the user belongs to (a handful)' },
  'src/ui/settings/SettingsScreen.tsx': { count: 1, reason: 'settings sections of the registry (a handful)' },
  'src/ui/screens/task/Tabs.tsx': { count: 1, reason: 'task tabs of the registry (a handful)' },
  'src/ui/screens/whiteboard/WhiteboardScreen.tsx': { count: 1, reason: 'the fixed whiteboard levels (org, group…)' },
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
  'src/ui/settings/sections/credentials/Mailbox.tsx': { count: 1, reason: 'the fixed fields of the mailbox form' },
  'src/ui/settings/sections/credentials/DevicePicker.tsx': {
    count: 1,
    reason: 'native <option>s of the devices of the account (one element each, no layout per row)',
  },
  'src/ui/settings/sections/github/ConnectFlow.tsx': {
    count: 1,
    reason: 'native <option>s of the devices of the account (one element each, no layout per row)',
  },
  'src/ui/settings/sections/github/Accounts.tsx': {
    count: 1,
    reason: 'GitHub accounts of one account, each connected by an explicit sign-in at GitHub (administrative set)',
  },
  'src/ui/composer/IdentityChoice.tsx': {
    count: 1,
    reason: 'native <option>s of the GitHub accounts the user may use (own and company ones, each an explicit sign-in)',
  },
  'src/ui/screens/github-accounts/CompanyAccountsScreen.tsx': {
    count: 1,
    reason:
      'company GitHub accounts of one org, each connected by an admin through a sign-in at GitHub (administrative set)',
  },
  'src/ui/screens/wg-settings/GithubDefault.tsx': {
    count: 1,
    reason: 'native <option>s of the company GitHub accounts of the org (each connected by an admin)',
  },
  'src/ui/composer/DeviceChoice.tsx': {
    count: 2,
    reason:
      'native <option>s of the devices assigned to one workgroup (administrative assignments, as GET ' +
      '/v1/workgroups/{wg}) and of the live VMs of one device (bounded by its VM limit)',
  },
  'src/ui/screens/devices/DevicesScreen.tsx': {
    count: 2,
    reason:
      "the capacity details of one device (closed list of fields) and its live VMs (bounded by the device's VM limit)",
  },
  'src/ui/browser/BrowserTab.tsx': {
    count: 2,
    reason:
      'its own window of the action record (RECORD_ROWS = 20 rows, older pages on request) and the three ' +
      'bandwidth choices (closed list)',
  },
  'src/ui/task-tabs/people.tsx': {
    count: 1,
    reason:
      'consumption per person (KOR-46): one row per member who caused charges in the 30 days or the task, at most ' +
      "the contract's 10 000 (the org's membership, complete by design like GET /v1/orgs/{org})",
  },
  'src/ui/settings/sections/language/index.tsx': { count: 1, reason: 'the supported locales (closed list)' },
  'src/ui/settings/sections/quotas/index.tsx': { count: 1, reason: 'quota windows of one provider (closed list)' },
  'src/ui/settings/sections/billing/index.tsx': {
    count: 4,
    reason:
      'consumption per day (at most 30) and per workgroup of the org, the price list in force, the newest 100 credit ' +
      'entries (credits_truncated says when older ones exist, hub billing/routes.py)',
  },
  'src/ui/chat-items/operation/Card.tsx': {
    count: 4,
    reason:
      'facts of one operation; one GitHub page of issues or pull requests and one page of comments (per_page at most ' +
      '100) and the labels of one entry (at most 100, runner github/issues.py)',
  },
  'src/ui/countdown/CountdownPanel.tsx': { count: 1, reason: 'the fixed steps of the countdown' },
  'src/ui/shell/header/bell.tsx': { count: 1, reason: 'at most 3 toasts (core/store/slices/notifications.ts)' },
  'src/ui/shell/header/credentials.tsx': { count: 1, reason: 'at most 3 toasts (core/store/slices/credentials.ts)' },
  'src/ui/screens/session/WorkvmPanel.tsx': {
    count: 1,
    reason: 'the newest PANEL_TESTS (50) test runs of one session, collapsed until opened',
  },
  'src/ui/settings/sections/model-quotas/index.tsx': {
    count: 2,
    reason: 'the quota windows of one own subscription and the operator subscriptions configured for the platform',
  },
  'src/ui/settings/sections/model/index.tsx': {
    count: 3,
    reason: 'the fixed access kinds, the models of the catalog for one access kind and the fixed fields of a key',
  },
  'src/ui/charts/DataTable.tsx': {
    count: 3,
    reason: 'at most TABLE_ROWS buckets of a chart range, a real table (header cells, rows, cells)',
  },
  'src/ui/screens/monitoring/EntityCharts.tsx': {
    count: 2,
    reason: 'the fixed chart ranges and the charts of one entity kind (closed lists)',
  },
  'src/ui/terminal/TerminalView.tsx': { count: 2, reason: 'the fixed modes and replay speeds of the terminal' },
  'src/ui/whiteboard/LineDiff.tsx': {
    count: 1,
    reason: 'lines of a clipped diff (useClipped: at most 20 000 characters at a time)',
  },
  'src/ui/screens/members/Chips.tsx': {
    count: 1,
    reason: 'workgroup chips of one row: the first 8, the rest (bounded by the org workgroups) on request',
  },
  'src/ui/screens/members/MembersScreen.tsx': { count: 1, reason: 'native <option>s of the org workgroups' },
  'src/ui/settings/ActiveOrg.tsx': { count: 1, reason: 'native <option>s of the orgs of the account (GET /v1/me)' },
  'src/ui/screens/members/InviteSection.tsx': {
    count: 2,
    reason: 'the org workgroups with their access and the fixed expiry choices (not the invitations)',
  },
  'src/ui/screens/members/AdmitSection.tsx': {
    count: 1,
    reason: 'the org workgroups with their access (the invited accounts are a Picker)',
  },
  'src/ui/screens/devices/AddDevice.tsx': { count: 1, reason: 'the org workgroups as checkboxes' },
  'src/ui/screens/devices/dialogs.tsx': {
    count: 1,
    reason: 'native <option>s of the org workgroups (the candidates are a Picker)',
  },
  'src/ui/screens/devices/parts.tsx': { count: 1, reason: 'the fixed expiry choices (EXPIRY_HOURS)' },
  'src/ui/screens/home/HomeScreen.tsx': {
    count: 3,
    reason:
      'the four counters and the four attention categories of the situation view (closed lists) and native ' +
      '<option>s of the writable workgroups',
  },
  'src/ui/screens/home/AttentionGroup.tsx': {
    count: 1,
    reason: 'a group of the situation view renders its own window of at most WINDOW entries, paged by the hub (HOT-05)',
  },
  'src/ui/screens/home/SessionTree.tsx': {
    count: 1,
    reason: 'the agents of one task, at most TREE_MAX shown and the rest counted (GET /v1/tasks/{task}: 100 sessions)',
  },
  'src/ui/screens/tasks/OverviewScreen.tsx': { count: 1, reason: 'native <option>s of the readable workgroups' },
  'src/ui/screens/tasks/TaskTable.tsx': { count: 1, reason: 'the task states (closed list)' },
  'src/ui/screens/audit/AuditView.tsx': {
    count: 3,
    reason:
      'filter choices: the closed action list and native <option>s of the org members and workgroups (one element ' +
      'each; the membership is complete by design, ADR-086 section 2)',
  },
  'src/ui/task-tabs/operations/OperationsTab.tsx': {
    count: 1,
    reason: 'transitions of one operation (at most 100, hub operations/read.py)',
  },
  'src/ui/settings/sections/agents/index.tsx': {
    count: 1,
    reason: 'agents the account paired, each an explicit pairing by the user',
  },
  'src/ui/screens/community/MessageActions.tsx': { count: 1, reason: 'the actions on one message (closed list)' },
  'src/ui/screens/community/parts.tsx': {
    count: 1,
    reason: 'text and link pieces of one message (at most 2 000 characters, 14-community.md section 3)',
  },
  'src/ui/screens/community/MutedUsers.tsx': {
    count: 1,
    reason: 'users a moderator muted, each an explicit moderator action (shown to moderators only, on request)',
  },
  'src/ui/pulls/LinkForm.tsx': {
    count: 2,
    reason:
      'native <option>s of the first page of the workgroup tasks (PAGE_SIZE, core/tasks/list.ts) and the tasks one ' +
      'pasted pull request is linked to (at most one per workgroup epoch the browser holds)',
  },
  'src/ui/screens/home/PullLine.tsx': {
    count: 1,
    reason:
      "at most LINE_MAX links of one task in a situation-view entry, the rest counted with a link to the task's tab",
  },
  'src/ui/screens/pulls/PullsScreen.tsx': {
    count: 1,
    reason: 'the workgroups the user reads as the level bar (navigation, like the phone menu Workgroups.tsx)',
  },
  'src/ui/screens/session/children/ChildrenTab.tsx': {
    count: 2,
    reason: 'subagents of one session: at most 10 per task and depth 2 (hub operations/limits.py)',
  },
  'src/ui/subscriptions/ProviderList.tsx': {
    count: 3,
    reason:
      "the user's own subscriptions (at most 40, subscriptions-list.response.json) by provider (a closed set), and " +
      'the windows of one provider (5 hours, week, week per model: at most 16 per reading)',
  },
  'src/ui/screens/subscriptions/SubscriptionsScreen.tsx': {
    count: 4,
    reason:
      "the fixed columns, the user's own subscriptions (at most 40, subscriptions-list.response.json) and the " +
      'windows of one reading in two cells (at most 16)',
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

/** How far after `.map(` a produced element is looked for (a callback of a few lines). */
const REACH = 400;

/** Lines of list markup made by `.map(` in `text`. */
function listMarkup(text: string): number[] {
  const lines = new Set<number>();
  for (const map of text.matchAll(/\.map\(/g)) {
    const after = text.slice(map.index, map.index + REACH);
    const next = after.indexOf('.map(', 1);
    const callback = next < 0 ? after : after.slice(0, next);
    // a list item, or any element with a `key` (the mark of a list made by map)
    if (/<(?:li|tr)[\s>]/.test(callback) || /<[A-Za-z][\w.]*\s[^<>]*?\bkey=/.test(callback)) {
      lines.add(lineOf(text, map.index));
    }
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

  it('finds every keyed element made by map, not only list markup', () => {
    expect(listMarkup('<select>\n{wgs.map((wg) => (\n  <option key={wg} value={wg} />\n))}</select>')).toEqual([2]);
    expect(listMarkup('rows.map((row) => <label key={row.id} className="x">{row.name}</label>)')).toEqual([1]);
    expect(listMarkup('rows.map((row) => <Fragment key={row}>{row}</Fragment>)')).toEqual([1]);
    expect(listMarkup('rows.map((row) => <ui.Row key={row} />)')).toEqual([1]);
    // no element, or an element without a key: not a list
    expect(listMarkup('rows.map((row) => ({ key: row }))')).toEqual([]);
    expect(listMarkup('rows.map((row) => <b>{row}</b>)')).toEqual([]);
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
