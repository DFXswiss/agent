// Volume gate (ADR-086 §4): the community with 2 000 messages over 20 days (`community_messages` of the seed). The
// browser user gets the community key once from the suite's keeper (first visit, not measured); the measured visit
// is a page load with the key held.
import { expect } from '@playwright/test';
import { test } from './fixtures.ts';
import { openView } from './views.ts';

export const COVERS = ['route:/community', 'list:community.messages'];

test('route:/community', async ({ volume }, info) => {
  await volume.open('/community');
  await expect(volume.page.getByTestId('community.composer')).toBeVisible({ timeout: 120_000 });
  await openView(volume, info, {
    id: 'route:/community',
    path: '/community',
    ready: (page) => page.locator('[data-testid="community.message"][data-status="verified"]').last(),
    primary: (page) => page.getByTestId('community.composer.text'),
    also: ['list:community.messages'],
  });
});
