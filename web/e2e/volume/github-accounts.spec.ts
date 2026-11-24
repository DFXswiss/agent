// Volume gate (ADR-086 §4): the company GitHub accounts of the volume org, opened by an admin of a large org. The list
// is an administrative set (each account connected by an admin through a sign-in at GitHub on a device of the org; GET
// /v1/me/github-identities is structural, tests/platform/hub/test_get_bounds.py), so the seed adds no accounts: the
// view is measured with the org's membership, workgroups and devices at volume.
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/o/:org/github'];

test('route:/o/:org/github', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/o/:org/github',
    path: orgPath(volume.data, '/github'),
    ready: (page) => page.getByTestId('company-github.empty').or(page.getByTestId('company-github.list')),
    primary: (page) => page.getByTestId('company-github.add'),
  });
});
