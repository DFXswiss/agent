// Volume gate (ADR-063 §4): the task list of a workgroup with 5 000 tasks (pages loaded while scrolling).
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/w/:wg', 'list:tasks.list'];

test('route:/o/:org/w/:wg', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/w/:wg',
    path: orgPath(volume.data, `/w/${volume.data.wg}`),
    ready: (page) => page.getByTestId('tasks.row').first(),
    primary: (page) => page.getByTestId('composer.prompt'),
    also: ['list:tasks.list'],
  });
});
