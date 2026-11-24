// Volume gate (ADR-063 §4): the home screen of an org whose default workgroup holds 5 000 tasks.
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org'];

test('route:/o/:org', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org',
    path: orgPath(volume.data),
    ready: (page) => page.getByTestId('tasks.row').first(),
    primary: (page) => page.getByTestId('composer.prompt'),
  });
});
