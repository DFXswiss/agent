// Volume gate (ADR-086 §4): the org's audit log (one row per stored event of the data set: over 100 000 rows).
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/audit', 'list:audit'];

test('route:/o/:org/audit', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/audit',
    path: orgPath(volume.data, '/audit'),
    ready: (page) => page.getByTestId('audit.row').first(),
    primary: (page) => page.getByTestId('audit.filter.apply'),
    also: ['list:audit'],
  });
});
