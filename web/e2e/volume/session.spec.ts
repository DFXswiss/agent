// Volume gate (ADR-063 §4): the session detail of the volume task's main session – its terminal (10 000 chunks,
// some with very long lines), its chat and its subagents (a chain deeper than the hub's limit, 31 children).
import { orgPath, test } from './fixtures.ts';
import { measureHere, openView } from './views.ts';
import { knownGap } from './gaps.ts';

export const COVERS = [
  'route:/o/:org/s/:session',
  'session:terminal',
  'session:chat',
  'list:session.chat',
  'session:children',
];

test('route:/o/:org/s/:session', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/s/:session',
    path: orgPath(volume.data, `/s/${volume.data.big_session}`),
    // the terminal (default tab) with its history loaded
    ready: (page) => page.locator('[data-testid="terminal"]:not(:has([data-testid="terminal.loading"]))'),
    primary: (page) => page.getByTestId('session.tabs.chat'),
    scroll: (page) => page.getByTestId('terminal.viewport'),
    also: ['session:terminal'],
  });
});

test('session:chat', async ({ volume }, info) => {
  knownGap(info, 'session:chat');
  await volume.open(orgPath(volume.data, `/s/${volume.data.big_session}`));
  const page = volume.page;
  await page.getByTestId('session.tabs.chat').click({ timeout: 60_000 });
  await measureHere(volume, info, Date.now(), {
    id: 'session:chat',
    ready: () => page.getByTestId('chat.item').first(),
    primary: () => page.getByTestId('session.tabs.children'),
    also: ['list:session.chat'],
  });
});

test('session:children', async ({ volume }, info) => {
  knownGap(info, 'session:children');
  await volume.open(orgPath(volume.data, `/s/${volume.data.big_session}`));
  const page = volume.page;
  await page.getByTestId('session.tabs.children').click({ timeout: 60_000 });
  await measureHere(volume, info, Date.now(), {
    id: 'session:children',
    ready: () => page.getByTestId('session.child').first(),
    primary: () => page.getByTestId('session.child.link').first(),
  });
});
