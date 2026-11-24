// Volume gate (ADR-086 §4): the security page (static, but loaded by a user of a large org).
import { test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/security'];

test('route:/security', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/security',
    path: '/security',
    anonymous: true,
    primary: (page) => page.getByTestId('security.compare'),
  });
});
