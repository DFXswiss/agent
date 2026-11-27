// Shared steps of the task specs: onboarding with a fresh platform device and sending a prompt from the home screen.
import { expect, type Page } from '@playwright/test';
import { addAuthenticator } from './authenticator.ts';
import { newDevice, TASK_PATH } from './director.ts';
import { type Onboarded, onboard } from './onboard.ts';

export const START_STEPS = [
  'vm_assigning',
  'vm_ready',
  'sandbox_ready',
  'agent_starting',
  'prompt_delivering',
] as const;

/** A new user in a private org; a new platform device is the first candidate (options of `newDevice`). */
export async function freshUser(
  page: Page,
  origin: string,
  device: { online?: boolean; poolReady?: number; free?: number } = {},
): Promise<Onboarded & { device: string }> {
  await addAuthenticator(page);
  const id = await newDevice(device);
  const onboarded = await onboard(page, { origin });
  return { ...onboarded, device: id };
}

/**
 * Sends `prompt` from the composer on the current page and returns the task ID of the task page it lands on. The page
 * opens on the tab "Claude Code" (KONZEPT E-43); with `tab` `chat` (the default) the step then opens the Chat tab, where
 * the specs read prompts, outputs and cards.
 */
export async function sendPrompt(page: Page, prompt: string, tab: 'chat' | 'terminal' = 'chat'): Promise<string> {
  await page.getByTestId('composer.prompt').fill(prompt);
  await page.getByTestId('composer.send').click();
  await expect(page).toHaveURL(TASK_PATH);
  const task = TASK_PATH.exec(page.url())![1]!;
  await expect(page.getByTestId('task.tabs.terminal')).toHaveAttribute('aria-current', 'page');
  if (tab === 'chat') await openChat(page);
  return task;
}

/** Opens the Chat tab of the task page (the second tab since KONZEPT E-43). */
export async function openChat(page: Page): Promise<void> {
  await page.getByTestId('task.tabs.chat').click();
  await expect(page.getByTestId('chat')).toBeVisible();
}

/** No spinner anywhere in the countdown panel (06-web.md sections 1 and 4.4). */
export async function expectNoSpinner(page: Page): Promise<void> {
  const panel = page.getByTestId('countdown.panel');
  await expect(panel.locator('.animate-spin, .animate-pulse, [role="progressbar"]')).toHaveCount(0);
}
