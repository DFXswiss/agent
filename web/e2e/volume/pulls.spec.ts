// Volume gate (ADR-086 §4): the pull requests of the volume workgroup (1 000 links over the hub's cursor pages) – the
// default workgroup's view, the same view by its workgroup, and the "Pull requests" tab of the big task (750 links).
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = [
  'route:/o/:org/pulls',
  'route:/o/:org/w/:wg/pulls',
  'list:pulls',
  'tab:pulls',
  'list:task.pulls',
];

test('route:/o/:org/pulls', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/pulls',
    path: orgPath(volume.data, '/pulls'),
    ready: (page) => page.getByTestId('pulls.item').first(),
    primary: (page) => page.getByTestId('pulls.link.input'),
    also: ['list:pulls'],
  });
});

test('route:/o/:org/w/:wg/pulls', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/w/:wg/pulls',
    path: orgPath(volume.data, `/w/${volume.data.wg}/pulls`),
    ready: (page) => page.getByTestId('pulls.item').first(),
    primary: (page) => page.getByTestId('pulls.link.input'),
  });
});

test('tab:pulls', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'tab:pulls',
    path: `${orgPath(volume.data, `/t/${volume.data.big_task}`)}?tab=pulls`,
    ready: (page) => page.getByTestId('task.pulls').getByTestId('pulls.item').first(),
    primary: (page) => page.getByTestId('task.pulls').getByTestId('pulls.link.input'),
    also: ['list:task.pulls'],
  });
});
