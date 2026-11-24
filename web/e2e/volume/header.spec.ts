// Volume gate (ADR-086 §4): every header item for a user with 2 000 notifications, two orgs and the platform role
// inviter, on the home screen of the volume org.
import { orgPath, test } from './fixtures.ts';
import { measureHere, openView } from './views.ts';

export const COVERS = [
  'header:bell',
  'header:org-switcher',
  'header:platform-invites',
  'header:credentials',
  'header:billing',
  'header:joined',
];

test('header:bell', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'header:bell',
    path: orgPath(volume.data),
    ready: (page) => page.getByTestId('bell.badge'),
    primary: (page) => page.getByTestId('bell.link'),
  });
  const page = volume.page;
  await measureHere(volume, info, Date.now(), {
    id: 'header:platform-invites',
    primary: () => page.getByTestId('shell.platform-invites'),
  });
  // the credential toasts, the credit badge (only while billing is active) and the joined notice have no control of
  // their own: the header around them stays usable
  for (const id of ['header:billing', 'header:joined'])
    await measureHere(volume, info, Date.now(), {
      id,
      ready: () => page.getByTestId('shell.header'),
      primary: () => page.getByTestId('shell.settings'),
    });
  await measureHere(volume, info, Date.now(), {
    id: 'header:credentials',
    ready: () => page.getByTestId('shell.header'),
    primary: () => page.getByTestId('shell.settings'),
  });
  const since = Date.now();
  await page.getByTestId('org-switcher.current').click({ timeout: 30_000 });
  await measureHere(volume, info, since, {
    id: 'header:org-switcher',
    ready: () => page.getByTestId('org-switcher.item').first(),
    primary: () => page.getByTestId('org-switcher.create.open'),
  });
});
