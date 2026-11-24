// Volume gate (ADR-086 §4): whiteboards with 300 entries on the org and the workgroup level, and the history of an
// entry with 200 versions of 64 KiB each.
import { orgPath, test } from './fixtures.ts';
import { measureHere, openView } from './views.ts';
import { knownGap } from './gaps.ts';

export const COVERS = [
  'route:/o/:org/whiteboard',
  'route:/o/:org/w/:wg/whiteboard',
  'list:whiteboard.entries',
  'view:whiteboard.history',
  'list:whiteboard.history',
];

test('route:/o/:org/whiteboard', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/whiteboard',
    path: orgPath(volume.data, '/whiteboard'),
    ready: (page) => page.getByTestId('whiteboard.item').first(),
    primary: (page) => page.getByTestId('whiteboard.create'),
    also: ['list:whiteboard.entries'],
  });
});

test('route:/o/:org/w/:wg/whiteboard', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/w/:wg/whiteboard',
    path: orgPath(volume.data, `/w/${volume.data.wg}/whiteboard`),
    ready: (page) => page.getByTestId('whiteboard.item').first(),
    primary: (page) => page.getByTestId('whiteboard.create'),
  });
});

test('view:whiteboard.history', async ({ volume }, info) => {
  knownGap(info, 'view:whiteboard.history');
  await volume.open(orgPath(volume.data, `/w/${volume.data.wg}/whiteboard`));
  const page = volume.page;
  const entry = page.locator(`[data-testid="whiteboard.item"][data-entry="${volume.data.whiteboard_entry}"]`);
  await entry.getByTestId('whiteboard.item.history').click({ timeout: 60_000 });
  await measureHere(volume, info, Date.now(), {
    id: 'view:whiteboard.history',
    ready: () => page.getByTestId('whiteboard.history.version').first(),
    primary: () => page.getByTestId('whiteboard.history.close'),
    scroll: () => page.getByTestId('whiteboard.history'),
    also: ['list:whiteboard.history'],
  });
});
