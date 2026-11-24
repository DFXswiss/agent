// Device reachability outside the device screen (concept section 7: reachability visible; KOR-40): the hub's
// presence of the org's devices (`GET /v1/orgs/{org}/devices`, `online`), read when mounted and every `refreshMs`.
//
// The hub pages the device list (ADR-086 section 2, `core/org/pages.ts`). A view asks for the devices it shows: the
// pages are read newest first until each of them was found or the list ends, at most `PRESENCE_MAX_PAGES` pages of
// `LIMIT_MAX` – the hub's cap of the device list (10 000) – so a device on an older page is found as well and the read
// stays bounded. A device the list does not name has no presence (the view shows none).
import { useEffect, useState } from 'react';
import { loadOrgDevices } from '../../../core/org/devices';
import { LIMIT_MAX } from '../../../core/org/pages';
import type { TasksContext } from '../../../core/tasks/pull';
import { useNow } from '../../countdown/useNow';
import { useAppEnv } from '../../shell/context';

export const PRESENCE_REFRESH_MS = 30_000;
/** The hub's cap of the device list (10 000) in pages of `LIMIT_MAX`. */
export const PRESENCE_MAX_PAGES = 10_000 / LIMIT_MAX;

/**
 * Device ID -> reachable for those of `devices` the hub lists in `org`: pages are read until every one of them was
 * found, the list ends or `PRESENCE_MAX_PAGES` were read.
 */
export async function readPresence(
  context: Pick<TasksContext, 'client'>,
  org: string,
  devices: readonly string[],
): Promise<ReadonlyMap<string, boolean>> {
  const wanted = new Set(devices);
  const found = new Map<string, boolean>();
  let before: string | null = null;
  for (let pages = 0; pages < PRESENCE_MAX_PAGES; pages += 1) {
    const page = await loadOrgDevices(context, org, before);
    for (const device of page.rows) if (wanted.has(device.device_id)) found.set(device.device_id, device.online);
    if (found.size === wanted.size || page.nextBefore === null) break;
    before = page.nextBefore;
  }
  return found;
}

/**
 * Device ID -> reachable for `devices` of `org` (the devices a view shows); null until read, when the read failed or
 * when no device is asked for (nothing is read then).
 */
export function useDevicePresence(
  org: string,
  devices: readonly string[],
  refreshMs: number = PRESENCE_REFRESH_MS,
): ReadonlyMap<string, boolean> | null {
  const { app } = useAppEnv();
  // one value for the same set of devices, whatever the order (device IDs carry no spaces)
  const wanted = [...new Set(devices)].sort().join(' ');
  const tick = useNow(wanted !== '', refreshMs);
  const [presence, setPresence] = useState<ReadonlyMap<string, boolean> | null>(null);
  useEffect(() => {
    if (wanted === '') return;
    let current = true;
    readPresence(app.tasks, org, wanted.split(' ')).then(
      (found) => current && setPresence(found),
      () => current && setPresence(null),
    );
    return () => {
      current = false;
    };
  }, [app, org, wanted, tick]);
  return wanted === '' ? null : presence;
}
