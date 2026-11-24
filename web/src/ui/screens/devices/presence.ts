// Device reachability outside the device screen (concept section 7: reachability visible; KOR-40): the hub's
// presence of the org's devices (`GET /v1/orgs/{org}/devices`, `online`), read when mounted and every `refreshMs`.
import { useEffect, useState } from 'react';
import { loadOrgDevices } from '../../../core/org/devices';
import { useNow } from '../../countdown/useNow';
import { useAppEnv } from '../../shell/context';

export const PRESENCE_REFRESH_MS = 30_000;

/** Device ID -> reachable for the devices of `org`; null until read (or when the read failed). */
export function useDevicePresence(
  org: string,
  refreshMs: number = PRESENCE_REFRESH_MS,
): ReadonlyMap<string, boolean> | null {
  const { app } = useAppEnv();
  const tick = useNow(true, refreshMs);
  const [presence, setPresence] = useState<ReadonlyMap<string, boolean> | null>(null);
  useEffect(() => {
    loadOrgDevices(app.tasks, org).then(
      (found) => setPresence(new Map(found.map((device) => [device.device_id, device.online]))),
      () => setPresence(null),
    );
  }, [app, org, tick]);
  return presence;
}
