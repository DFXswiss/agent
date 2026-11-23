// The app's own navigation in both Playwright projects (SCN-08; 06-web.md section 12.1): from `md` (768 px) the
// navigation row/sidebar `shell.nav.<id>`, below it the bottom bar `shell.bottom.<id>` (home, tasks, notifications,
// monitoring) and the "More" sheet `shell.more.<id>` for every other entry. Specs name the entry; the viewport of the
// project decides which control is used (the only difference between the desktop and the mobile project).
import type { Page } from '@playwright/test';

export const MD = 768;
const BOTTOM = new Set(['home', 'tasks', 'notifications', 'monitoring']);

/** Opens the navigation entry `id` (e.g. `home`, `whiteboard`, `audit`) the way a user of this viewport does. */
export async function navTo(page: Page, id: string): Promise<void> {
  if (page.viewportSize()!.width >= MD) {
    await page.getByTestId(`shell.nav.${id}`).click();
  } else if (BOTTOM.has(id)) {
    await page.getByTestId(`shell.bottom.${id}`).click();
  } else {
    await page.getByTestId('shell.bottom.more').click();
    await page.getByTestId(`shell.more.${id}`).click();
  }
}
