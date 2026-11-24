// Volume gate (ADR-086 §4): the workgroups of the volume org (their members and devices at volume).
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/workgroups', 'list:workgroups'];

test('route:/o/:org/workgroups', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/workgroups',
    path: orgPath(volume.data, '/workgroups'),
    ready: (page) => page.getByTestId('workgroups.item').first(),
    primary: (page) => page.getByTestId('workgroups.create.open'),
    also: ['list:workgroups'],
  });
});
