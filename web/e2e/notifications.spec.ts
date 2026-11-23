// Notifications (06-web.md section 6.2, ADR-020) against the real hub with two browser contexts: an entry sent with
// "Notify participants" reaches the member live – badge on the bell, a toast in a polite live region that closes by
// itself after 6 s, a row in the center with the client-built text; a click marks it read and opens the board; an
// entry without the checkbox notifies nobody; "Mark all read" clears the badge; the center works at phone size.
import { devices, type Page } from '@playwright/test';
import { expect, test } from './fixtures/test.ts';
import { navigateInApp, type Team, team } from './helpers/whiteboard.ts';

/** Ada writes an org entry, optionally with the notification. */
async function write(page: Page, org: string, text: string, notify: boolean): Promise<void> {
  await navigateInApp(page, `/o/${org}/whiteboard`);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill(text);
  if (notify) await page.getByTestId('whiteboard.editor.notify').check();
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(page.getByTestId('whiteboard.item').filter({ hasText: text })).toBeVisible();
}

const ORG_TEXT = 'New whiteboard entry in organization “Private”';

test('a notified entry reaches the member live: badge, toast, center, read @R-12.8', async ({
  page,
  baseURL,
  newContext,
}) => {
  const { org, bob }: Team = await team(page, newContext, baseURL!);
  await bob.page.getByTestId('bell.link').click();
  await expect(bob.page).toHaveURL(/\/notifications$/);
  await expect(bob.page.getByTestId('notifications.empty')).toBeVisible();
  await expect(bob.page.getByTestId('bell.badge')).toHaveCount(0);
  await expect(bob.page.getByTestId('bell.link')).toHaveAttribute('aria-label', 'Notifications');
  // without the checkbox nobody is notified
  await write(page, org, 'Quiet entry', false);
  // with it, Bob's browser shows badge, toast and row at once
  await write(page, org, 'Loud entry', true);
  await expect(bob.page.getByTestId('bell.badge')).toHaveText('1');
  await expect(bob.page.getByTestId('bell.link')).toHaveAttribute('aria-label', 'Notifications, 1 unread');
  await expect(bob.page.getByTestId('bell.toasts')).toHaveAttribute('aria-live', 'polite');
  await expect(bob.page.getByTestId('bell.toast.open')).toHaveText(ORG_TEXT);
  const row = bob.page.getByTestId('notifications.item');
  await expect(row).toHaveCount(1);
  await expect(row).toHaveAttribute('data-unread', 'true');
  await expect(row.getByTestId('notifications.text')).toHaveText(ORG_TEXT);
  await expect(row.getByTestId('notifications.item.time')).toHaveText(/^\d{2}:\d{2}:\d{2}\.\d{3}$/);
  // the toast closes by itself after 6 s
  await expect(bob.page.getByTestId('bell.toast')).toHaveCount(0, { timeout: 9_000 });
  // the author is not notified of her own entry: Ada's only notification is Bob's accepted invitation
  await expect(page.getByTestId('bell.badge')).toHaveText('1');
  await page.getByTestId('bell.link').click();
  await expect(page.getByTestId('notifications.item')).toHaveCount(1);
  await expect(page.getByTestId('notifications.item')).toHaveAttribute('data-kind', 'invite.accepted');
  await expect(page.getByTestId('notifications.text')).toHaveText('Bob Member accepted the invitation to “Private”');
  await page.getByTestId('notifications.item').click();
  await expect(page).toHaveURL(new RegExp(`/o/${org}/members$`));
  // a click marks it read and opens the board
  await row.click();
  await expect(bob.page).toHaveURL(new RegExp(`/o/${org}/whiteboard$`));
  await expect(bob.page.getByTestId('whiteboard.item').filter({ hasText: 'Loud entry' })).toBeVisible();
  await expect(bob.page.getByTestId('bell.badge')).toHaveCount(0);
  await bob.page.getByTestId('bell.link').click();
  await expect(bob.page.getByTestId('notifications.item')).toHaveAttribute('data-unread', 'false');
  await expect(bob.page.getByTestId('notifications.mark-all')).toHaveCount(0);
  // the hub row is content-free and read
  const stored = (await bob.user.request('GET', '/v1/notifications')) as { notifications: Record<string, unknown>[] };
  expect(stored.notifications).toHaveLength(1);
  expect(stored.notifications[0]).toMatchObject({ kind: 'whiteboard.entry', org_id: org, wg_id: null });
  expect(stored.notifications[0]!.read_at).not.toBeNull();
  expect(JSON.stringify(stored)).not.toContain('Loud entry');
});

test('a toast opens its target; mark all read clears the badge @R-12.8', async ({ page, baseURL, newContext }) => {
  const { org, research, bob } = await team(page, newContext, baseURL!);
  // a workgroup entry with notify: the toast names the workgroup and opens its board
  await navigateInApp(page, `/o/${org}/w/${research}/whiteboard`);
  await page.getByTestId('whiteboard.create').click();
  await page.getByTestId('whiteboard.editor.text').fill('Research update');
  await page.getByTestId('whiteboard.editor.notify').check();
  await page.getByTestId('whiteboard.editor.save').click();
  await expect(bob.page.getByTestId('bell.toast.open')).toHaveText('New whiteboard entry in workgroup “Research”');
  await bob.page.getByTestId('bell.toast.open').click();
  await expect(bob.page).toHaveURL(new RegExp(`/o/${org}/w/${research}/whiteboard$`));
  await expect(bob.page.getByTestId('bell.badge')).toHaveCount(0);
  // two more; the close button dismisses a toast; mark all read
  await write(page, org, 'One', true);
  await expect(bob.page.getByTestId('bell.badge')).toHaveText('1');
  await bob.page.getByTestId('bell.toast.close').click();
  await expect(bob.page.getByTestId('bell.toast')).toHaveCount(0);
  await write(page, org, 'Two', true);
  await expect(bob.page.getByTestId('bell.badge')).toHaveText('2');
  await bob.page.getByTestId('bell.link').click();
  await expect(bob.page.getByTestId('notifications.item')).toHaveCount(3);
  await bob.page.getByTestId('notifications.mark-all').click();
  await expect(bob.page.getByTestId('bell.badge')).toHaveCount(0);
  await expect(bob.page.locator('[data-testid="notifications.item"][data-unread="true"]')).toHaveCount(0);
  // after a reload the hub's count is 0 as well
  await bob.page.reload();
  await bob.page.getByTestId('login.submit').click();
  await expect(bob.page.getByTestId('notifications.item')).toHaveCount(3);
  await expect(bob.page.getByTestId('bell.badge')).toHaveCount(0);
});

test.describe('on a phone', () => {
  const { defaultBrowserType: _browser, ...pixel7 } = devices['Pixel 7'];
  test.use(pixel7);

  test('bell, toast and center work at phone size @R-12.8', async ({ page, baseURL, newContext }) => {
    const { org, bob } = await team(page, () => newContext(pixel7), baseURL!);
    // the smallest screen of 06-web.md section 1.4, where the header wraps into two rows
    await bob.page.setViewportSize({ width: 360, height: 640 });
    // Read in one frame (the header items load and may wrap at any time): the space between the lowest header item in
    // the flow that is not the toast list and the header's bottom edge, and the height of the toast, if any.
    const layout = () =>
      bob.page.evaluate(() => {
        const header = document.querySelector('[data-testid="shell.header"]')!;
        const list = header.querySelector('[data-testid="bell.toasts"]');
        let lowest = 0;
        for (const child of Array.from(header.children))
          if (child !== list && !['fixed', 'absolute'].includes(getComputedStyle(child).position))
            lowest = Math.max(lowest, child.getBoundingClientRect().bottom);
        const toast = list?.querySelector('[data-testid="bell.toast"]')?.getBoundingClientRect();
        return { below: header.getBoundingClientRect().bottom - lowest, toast: toast?.height ?? 0 };
      });
    // the empty live region adds no row: below the lowest header item there is only the padding and the border
    expect((await layout()).below).toBeLessThanOrEqual(9);
    await write(page, org, 'Phone ping', true);
    await expect(bob.page.getByTestId('bell.badge')).toHaveText('1');
    // the toast covers nothing: neither the header's controls nor the bottom of the screen, where the bottom bar and
    // a page's last actions (the chat composer's Send) are; it is the header's last row while it is shown
    const toast = bob.page.getByTestId('bell.toast');
    // measured once the text is final (the names come in after the neutral fallback, the toast may change its height)
    await expect(bob.page.getByTestId('bell.toast.open')).toHaveText('New whiteboard entry in organization “Private”');
    const box = (await toast.boundingBox())!;
    expect(box.y + box.height).toBeLessThanOrEqual(640 / 2);
    // in the flow, however many rows the header wraps into: the toast is a row of its own below every header item
    const shown = await layout();
    expect(shown.toast).toBeGreaterThan(0);
    expect(shown.below).toBeGreaterThanOrEqual(shown.toast);
    for (const id of ['shell.brand', 'bell.link', 'shell.settings', 'shell.bottom']) {
      const other = (await bob.page.getByTestId(id).boundingBox())!;
      const apart = box.y + box.height <= other.y || other.y + other.height <= box.y;
      expect(apart, `toast overlaps ${id}`).toBe(true);
    }
    await bob.page.getByTestId('bell.toast.close').click();
    await expect(toast).toHaveCount(0);
    // without a toast the header has no extra row again
    expect((await layout()).below).toBeLessThanOrEqual(9);
    await bob.page.getByTestId('bell.link').click();
    await expect(bob.page.getByTestId('notifications.item')).toHaveCount(1);
    expect(bob.page.viewportSize()!.width).toBeLessThan(500);
    await expect(bob.page.getByTestId('bell.link')).toBeInViewport();
  });
});
