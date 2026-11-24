// Volume gate (ADR-086 §4): signing in (passkey unlock after a page load) as an admin of the volume org until its home
// screen is usable – the unlock verifies the org's log (members, devices, invitations at volume) and loads the views'
// first pages.
import { measure } from './budget.ts';
import { orgPath, test } from './fixtures.ts';
import { knownGap } from './gaps.ts';

export const COVERS = ['flow:unlock'];

test('flow:unlock', async ({ volume }, info) => {
  knownGap(info, 'flow:unlock');
  const since = await volume.open(orgPath(volume.data));
  const page = volume.page;
  await measure(page, info, 'flow:unlock', {
    since,
    ready: page.getByTestId('attention.item').first(),
    primary: page.getByTestId('composer.prompt'),
  });
});
