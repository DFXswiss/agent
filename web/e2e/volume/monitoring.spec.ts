// Volume gate (ADR-086 §4): monitoring of the org (the active sessions of the data set on its platform device).
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/monitoring', 'list:monitoring.tree'];

test('route:/o/:org/monitoring', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/monitoring',
    path: orgPath(volume.data, '/monitoring'),
    // loaded: the screen without its loading line
    primary: (page) => page.locator('[data-testid="monitoring.screen"]:not(:has([data-testid="monitoring.loading"]))'),
    also: ['list:monitoring.tree'],
  });
});
