// The device double of the suite (scripts/hub_test_director.py, decision WEB-04): platform devices with real keys and
// scripted session transitions, as the runner would report them. Specs that use it run in the `devices` project, one
// at a time: every new device takes the first place among the platform candidates.
import { expect } from '@playwright/test';
import { DIRECTOR_ORIGIN } from '../fixtures/env.ts';

async function call(method: string, path: string, body?: unknown): Promise<unknown> {
  const response = await fetch(`${DIRECTOR_ORIGIN}${path}`, {
    method,
    headers: { 'content-type': 'application/json' },
    ...(body !== undefined && { body: JSON.stringify(body) }),
  });
  const data = (await response.json()) as unknown;
  if (!response.ok) throw new Error(`director ${method} ${path}: ${JSON.stringify(data)}`);
  return data;
}

/** A new certified platform device (online by default; `poolReady` 0 gives an 8 s estimate, more a 3 s one; `free` 0
 *  leaves it without a free slot). */
export async function newDevice(
  options: { online?: boolean; poolReady?: number; free?: number } = {},
): Promise<string> {
  const answer = (await call('POST', '/devices', {
    online: options.online ?? true,
    pool_ready: options.poolReady ?? 0,
    free: options.free ?? 8,
  })) as { device_id: string };
  return answer.device_id;
}

export async function setOnline(device: string, online: boolean): Promise<void> {
  await call('POST', `/devices/${device}/online`, { online });
}

export interface MainSession {
  readonly session_id: string;
  readonly device_id: string | null;
  readonly state: string;
}

/** The task's main session (waits until the task exists and, with `placed`, until it has a device). */
export async function mainSession(task: string, placed = true): Promise<MainSession> {
  let found: MainSession | null = null;
  await expect
    .poll(
      async () => {
        found = (await call('GET', `/tasks/${task}/session`).catch(() => null)) as MainSession | null;
        return found !== null && (!placed || found.device_id !== null);
      },
      { timeout: 20_000 },
    )
    .toBe(true);
  return found!;
}

/** Reports `states` for the session, the first at once, then one every `intervalMs` (`reason` for a final `failed`). */
export async function walk(
  session: string,
  states: readonly string[],
  options: { reason?: string; intervalMs?: number } = {},
): Promise<void> {
  await call('POST', `/sessions/${session}/transitions`, {
    states,
    reason: options.reason ?? null,
    interval_ms: options.intervalMs ?? 0,
  });
}

/** The task ID of a task page URL. */
export const TASK_PATH = /\/o\/[0-9a-f-]{36}\/t\/([0-9a-f-]{36})$/;
