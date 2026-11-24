// Volume gate (ADR-063 §4): the security page (static, but loaded by a user of a large org).
import { test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/security'];

test('route:/security', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/security',
    path: '/security',
    primary: (page) => page.getByTestId('security.compare'),
  });
});
