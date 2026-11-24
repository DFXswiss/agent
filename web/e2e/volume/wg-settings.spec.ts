// Volume gate (ADR-063 §4): the settings of the big workgroup (its scope holds over 100 000 events).
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/w/:wg/settings'];

test('route:/o/:org/w/:wg/settings', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/w/:wg/settings',
    path: orgPath(volume.data, `/w/${volume.data.wg}/settings`),
    ready: (page) => page.getByTestId('wg-settings.form'),
    primary: (page) => page.getByTestId('wg-settings.save'),
  });
});
