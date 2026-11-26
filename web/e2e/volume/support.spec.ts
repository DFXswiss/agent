// Volume gate (ADR-086 §4): the support area (ADR-096) over the big workgroup. The volume user becomes a person of DFX
// support exactly as the operator makes one (root-signed `user_certify` and `role_grant support`) and releases the big
// workgroup through its settings (once per run: the second project finds it released); then the releases, the big
// workgroup's task list and a task's conversation are measured as support sees them.
import { expect } from '@playwright/test';
import { appendPlatform } from '../helpers/platform.ts';
import { PageUser } from '../helpers/user.ts';
import { orgPath, test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = [
  'route:/support',
  'route:/support/:org/:wg',
  'route:/support/:org/:wg/:task',
  'list:support.releases',
  'list:support.tasks',
];

test.beforeAll(async ({ volumeUser: volume, baseURL }) => {
  test.setTimeout(600_000);
  await volume.open('/settings');
  const user = await PageUser.open(volume.page, volume.recoveryCode);
  const platform = await user.platform();
  if (!platform.supportRoster().has(user.userId)) {
    await appendPlatform(baseURL!, [
      ...(platform.userCertified(user.userId)
        ? []
        : [{ type: 'user_certify', user: user.userId, pub: user.identity.public() }]),
      { type: 'role_grant', subject: user.userId, role: 'support' },
    ]);
  }
  // a page load reads the platform log with the role; the release, unless the other project gave it already
  await volume.open(orgPath(volume.data, `/w/${volume.data.wg}/settings`));
  const state = volume.page.getByTestId('support.section.state');
  await expect(state).toBeVisible({ timeout: 300_000 });
  if ((await state.getAttribute('data-release')) === 'none') {
    await volume.page.getByTestId('support.section.read').click();
    await volume.page.getByTestId('support.dialog.confirm').click();
    await expect(volume.page.getByTestId('support.section.done')).toBeVisible({ timeout: 300_000 });
  }
});

test('route:/support', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/support',
    path: '/support',
    ready: (page) => page.getByTestId('support.release').first(),
    primary: (page) => page.getByTestId('support.area.reload'),
    also: ['list:support.releases'],
  });
});

test('route:/support/:org/:wg', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/support/:org/:wg',
    path: `/support/${volume.data.org}/${volume.data.wg}`,
    ready: (page) => page.getByTestId('support.task').first(),
    primary: (page) => page.getByTestId('support.back'),
    also: ['list:support.tasks'],
  });
});

test('route:/support/:org/:wg/:task', async ({ volume }, info) => {
  await openView(volume, info, {
    id: 'route:/support/:org/:wg/:task',
    path: `/support/${volume.data.org}/${volume.data.wg}/${volume.data.big_task}`,
    ready: (page) => page.getByTestId('chat.item').first(),
    primary: (page) => page.getByTestId('support.task.back'),
  });
});
