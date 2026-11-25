// Volume gate (ADR-086 §4): the page "My subscriptions" (KOR-68) for a member of a large org – its own data only, so
// the size of the org must not slow it down.
import { test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/subscriptions'];

test('route:/subscriptions', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/subscriptions',
    path: '/subscriptions',
    ready: (page) => page.getByTestId('subscriptions.page'),
    primary: (page) => page.getByTestId('subscriptions.add'),
  });
});
