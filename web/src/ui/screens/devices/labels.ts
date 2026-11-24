// Device names outside the device screen (06-web.md sections 5.3, 5.4, 8.1, 8.2): a device is named by its label from
// the newest verified `device.profile` event of its org (`selectDeviceLabels`, pulled once per org when the org's log
// is verified), and only while none was read by "Device without a name" – never by an ID (ADR-067).
import { useEffect, useMemo, useRef } from 'react';
import { meOrgs } from '../../../core/account/session';
import { DEVICE_PROFILE, selectDeviceLabels } from '../../../core/org/devices';
import { orgEntry } from '../../../core/store/slices/orgs';
import { pullOnce } from '../../../core/tasks/pull';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';

/** The name of device `id`: its label, else "Device without a name". */
export type DeviceNamer = (id: string) => string;

/** Names devices by their labels in `orgs`; the profiles of each verified org are pulled once. */
export function useDeviceNames(orgs: readonly string[]): DeviceNamer {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const key = orgs.join(' ');
  const verified = useStore(
    (state) =>
      key
        .split(' ')
        .filter((org) => orgEntry(state.orgs, org).log !== null)
        .join(' '),
    app.store,
  );
  useEffect(() => {
    for (const org of verified.split(' ').filter(Boolean)) {
      pullOnce(app.tasks, org, org, { kinds: [DEVICE_PROFILE] }).catch(() => 0);
    }
  }, [app, verified]);
  // the label maps of the orgs, the same array while none of them changed (other events do not re-render)
  const last = useRef<readonly ReadonlyMap<string, string>[]>([]);
  const maps = useStore((state) => {
    const next = key.split(' ').map((org) => selectDeviceLabels(org)(state));
    const same = next.length === last.current.length && next.every((labels, i) => labels === last.current[i]);
    if (!same) last.current = next;
    return last.current;
  }, app.store);
  return useMemo(
    () => (id: string) => {
      for (const labels of maps) {
        const label = labels.get(id);
        if (label !== undefined) return label;
      }
      return t('names.device.unknown');
    },
    [maps, t],
  );
}

/** Names devices by their labels in any org of the user (the device choices of the settings span all orgs). */
export function useProfileDeviceNames(): DeviceNamer {
  const { app } = useAppEnv();
  const orgs = useStore(
    (state) =>
      meOrgs(state.me.profile)
        .map((org) => org.org_id)
        .join(' '),
    app.store,
  );
  return useDeviceNames(orgs.split(' ').filter(Boolean));
}
