// View types of the UI health report (ADR-086 section 5): the closed list of `contracts/enums/ui-views.json`, never an
// ID or a path. `VIEW_PATTERNS` mirrors the contract's `patterns` as data (core does not import the UI; a test compares
// both with the contract and with the routes of `ui/screens/*/routes.tsx`): a pattern is a route of the router, with
// `#<tab>` for a tab of the task page or the session detail (`?tab=`; an absent or unknown tab is the first tab listed,
// as the tab bar resolves it). Every other location – the start redirect, the not-found page – is `other`.

export const VIEW_PATTERNS = {
  home: ['/o/:org'],
  tasks: ['/o/:org/w/:wg'],
  'tasks.overview': ['/o/:org/tasks'],
  'task.terminal': ['/o/:org/t/:task#terminal'],
  'task.chat': ['/o/:org/t/:task#chat'],
  'task.sessions': ['/o/:org/t/:task#sessions'],
  'task.pulls': ['/o/:org/t/:task#pulls'],
  'task.operations': ['/o/:org/t/:task#operations'],
  'task.charges': ['/o/:org/t/:task#charges'],
  'task.whiteboard': ['/o/:org/t/:task#whiteboard'],
  'task.audit': ['/o/:org/t/:task#audit'],
  'session.terminal': ['/o/:org/s/:session#terminal'],
  'session.chat': ['/o/:org/s/:session#chat'],
  'session.children': ['/o/:org/s/:session#children'],
  'session.browser': ['/o/:org/s/:session#browser'],
  whiteboard: ['/o/:org/whiteboard', '/o/:org/w/:wg/whiteboard'],
  notifications: ['/notifications'],
  community: ['/community'],
  members: ['/o/:org/members'],
  devices: ['/o/:org/devices'],
  github: ['/o/:org/github'],
  workgroups: ['/o/:org/workgroups'],
  pulls: ['/o/:org/pulls', '/o/:org/w/:wg/pulls'],
  'wg-settings': ['/o/:org/w/:wg/settings'],
  monitoring: ['/o/:org/monitoring'],
  audit: ['/o/:org/audit'],
  settings: ['/settings', '/subscriptions'],
  'platform-invites': ['/platform/invites'],
  security: ['/security'],
  invite: ['/invite'],
  login: ['/login'],
  recover: ['/recover'],
  connect: ['/connect'],
  // the support area of DFX support persons (ADR-096) is reported as `other`: a view type of its own would need the hub
  other: ['/', '*', '/support', '/support/:org/:wg', '/support/:org/:wg/:task'],
} as const satisfies Record<string, readonly string[]>;

export type ViewName = keyof typeof VIEW_PATTERNS;

/** The view types in the order of the contract. */
export const VIEW_NAMES = Object.keys(VIEW_PATTERNS) as ViewName[];

/** The view type of every location no pattern names. */
export const FALLBACK: ViewName = 'other';

const segments = (path: string): string[] => path.split('/').filter((part) => part !== '');

const ENTRIES = VIEW_NAMES.flatMap((view) =>
  (VIEW_PATTERNS[view] as readonly string[])
    .filter((pattern) => pattern !== '*')
    .map((pattern) => {
      const [route, tab] = pattern.split('#');
      return { view, pattern: segments(route!), tab: tab ?? null };
    }),
);

function matches(pattern: readonly string[], path: readonly string[]): boolean {
  return pattern.length === path.length && pattern.every((part, i) => part.startsWith(':') || part === path[i]);
}

/** The view type of a location (`location.pathname`, `location.search`). */
export function viewOf(pathname: string, search: string): ViewName {
  const path = segments(pathname);
  const found = ENTRIES.filter((entry) => matches(entry.pattern, path));
  const wanted = new URLSearchParams(search).get('tab');
  return (found.find((entry) => entry.tab === wanted) ?? found[0])?.view ?? FALLBACK;
}

/** Whether a string is one of the view types. */
export const isViewName = (value: string): value is ViewName => Object.hasOwn(VIEW_PATTERNS, value);
