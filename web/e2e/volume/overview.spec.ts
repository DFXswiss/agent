// Volume gate (ADR-086 §4): the task overview of the org (every readable workgroup; 5 000 tasks in one).
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/tasks', 'list:tasks.overview'];

test('route:/o/:org/tasks', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/tasks',
    path: orgPath(volume.data, '/tasks'),
    ready: (page) => page.getByTestId('tasks.row').first(),
    primary: (page) => page.getByTestId('tasks.search'),
    also: ['list:tasks.overview'],
  });
});
