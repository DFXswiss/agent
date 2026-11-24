// U-WEB-ui-health (KOR-35, ADR-086 section 5): the closed list of view types and `viewOf` – exactly the types and
// patterns of contracts/enums/ui-views.json, every route of the web client covered, the task page and the session
// detail by their tab, anything else `other`; IDs never reach a view name.
import { readdirSync, readFileSync } from 'node:fs';
import { join } from 'node:path';
import { describe, expect, it } from 'vitest';
import { BUCKETS_MS, MAX_BLOCKED_MS, MAX_COUNT } from '../../../src/core/monitoring/longtasks';
import { FALLBACK, isViewName, VIEW_NAMES, VIEW_PATTERNS, viewOf } from '../../../src/core/monitoring/views';
import { readSource, WEB_DIR } from '../../helpers';

const contract = JSON.parse(readFileSync(join(WEB_DIR, '../contracts/enums/ui-views.json'), 'utf8')) as {
  views: string[];
  fallback: string;
  patterns: Record<string, string[]>;
  buckets_ms: number[];
  limits: Record<string, number>;
};

describe('the contract', () => {
  it('has exactly the view types, patterns, buckets and limits of contracts/enums/ui-views.json', () => {
    expect(VIEW_NAMES).toEqual(contract.views);
    expect(FALLBACK).toBe(contract.fallback);
    expect(VIEW_PATTERNS).toEqual(contract.patterns);
    expect([...BUCKETS_MS]).toEqual(contract.buckets_ms);
    expect(BUCKETS_MS.length - 1).toBe(contract.limits.max_bucket);
    expect(MAX_COUNT).toBe(contract.limits.count);
    expect(MAX_BLOCKED_MS).toBe(contract.limits.blocked_ms);
    expect(VIEW_NAMES.length).toBeLessThanOrEqual(contract.limits.views_per_report!);
  });
});

describe('viewOf', () => {
  it.each([
    ['/', '', 'other'],
    ['/login', '?next=x', 'login'],
    ['/invite', '', 'invite'],
    ['/recover', '', 'recover'],
    ['/connect', '', 'connect'],
    ['/security', '', 'security'],
    ['/settings', '', 'settings'],
    ['/notifications', '?tab=chat', 'notifications'],
    ['/community', '', 'community'],
    ['/platform/invites', '', 'platform-invites'],
    ['/o/org-1', '', 'home'],
    ['/o/org-1/', '', 'home'],
    ['/o/org-1/tasks', '', 'tasks.overview'],
    ['/o/org-1/w/wg-1', '', 'tasks'],
    ['/o/org-1/whiteboard', '', 'whiteboard'],
    ['/o/org-1/w/wg-1/whiteboard', '', 'whiteboard'],
    ['/o/org-1/workgroups', '', 'workgroups'],
    ['/o/org-1/pulls', '', 'pulls'],
    ['/o/org-1/w/wg-1/pulls', '', 'pulls'],
    ['/o/org-1/w/wg-1/settings', '', 'wg-settings'],
    ['/o/org-1/members', '', 'members'],
    ['/o/org-1/devices', '', 'devices'],
    ['/o/org-1/github', '', 'github'],
    ['/o/org-1/audit', '', 'audit'],
    ['/o/org-1/monitoring', '', 'monitoring'],
    ['/o/org-1/t/task-1', '', 'task.chat'],
    ['/o/org-1/t/task-1', '?tab=sessions', 'task.sessions'],
    ['/o/org-1/t/task-1', '?tab=pulls', 'task.pulls'],
    ['/o/org-1/t/task-1', '?tab=operations', 'task.operations'],
    ['/o/org-1/t/task-1', '?tab=charges', 'task.charges'],
    ['/o/org-1/t/task-1', '?tab=whiteboard', 'task.whiteboard'],
    ['/o/org-1/t/task-1', '?tab=audit', 'task.audit'],
    ['/o/org-1/t/task-1', '?tab=terminal', 'task.chat'],
    ['/o/org-1/s/sess-1', '', 'session.terminal'],
    ['/o/org-1/s/sess-1', '?tab=chat', 'session.chat'],
    ['/o/org-1/s/sess-1', '?x=1&tab=children', 'session.children'],
    ['/o/org-1/s/sess-1', '?tab=browser', 'session.browser'],
    ['/o/org-1/s/sess-1', '?tab=nope', 'session.terminal'],
    ['/nowhere', '', 'other'],
    ['/o/org-1/t', '', 'other'],
    ['/o/org-1/t/task-1/extra', '', 'other'],
    ['/platform', '', 'other'],
  ])('maps %s%s to %s', (pathname, search, view) => {
    expect(viewOf(pathname, search)).toBe(view);
  });

  it('knows the names of the closed list only', () => {
    expect(isViewName('task.chat')).toBe(true);
    expect(isViewName('task.nope')).toBe(false);
    expect(isViewName('toString')).toBe(false);
  });
});

describe('the patterns mirror the UI', () => {
  const screens = join(WEB_DIR, 'src/ui/screens');
  const routes = new Set(
    Object.values(VIEW_PATTERNS)
      .flat()
      .map((pattern) => pattern.split('#')[0]!),
  );
  const tabsOf = (route: string) =>
    Object.values(VIEW_PATTERNS)
      .flat()
      .filter((pattern) => pattern.startsWith(`${route}#`))
      .map((pattern) => pattern.split('#')[1]);

  it('names every route pattern of ui/screens', () => {
    const paths = readdirSync(screens, { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .flatMap((entry) => {
        const file = `src/ui/screens/${entry.name}/routes.tsx`;
        const source = readdirSync(join(screens, entry.name)).includes('routes.tsx') ? readSource(file) : '';
        return [...source.matchAll(/path: '([^']*)'/g)].map((match) => match[1]!);
      });
    expect(paths.length).toBeGreaterThan(20);
    expect([...routes].sort()).toEqual([...new Set(paths)].sort());
  });

  it('lists the task tabs and the session tabs in their order, the default first', () => {
    const taskTabs = readdirSync(join(WEB_DIR, 'src/ui/task-tabs'), { withFileTypes: true })
      .filter((entry) => entry.isDirectory())
      .map((entry) => {
        const source = readSource(`src/ui/task-tabs/${entry.name}/index.tsx`);
        const [, id, order] = /id: '([^']+)', order: (\d+)/.exec(source)!;
        return { id: id!, order: Number(order) };
      })
      .sort((a, b) => a.order - b.order)
      .map((tab) => tab.id);
    expect(tabsOf('/o/:org/t/:task')).toEqual(taskTabs);
    const sessionTabs = tabsOf('/o/:org/s/:session').map((tab) => `'${tab}'`);
    expect(readSource('src/ui/screens/session/SessionScreen.tsx')).toContain(
      `const TAB_IDS = [${sessionTabs.join(', ')}] as const;`,
    );
  });
});
