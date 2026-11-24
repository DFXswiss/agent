// Volume gate (ADR-086 §4): the home screen of an org whose default workgroup holds 5 000 tasks.
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org'];

test('route:/o/:org', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org',
    path: orgPath(volume.data),
    // the situation view: its groups (running, recently finished …) hold the volume tasks
    ready: (page) => page.getByTestId('attention.item').first(),
    primary: (page) => page.getByTestId('composer.prompt'),
  });
});
