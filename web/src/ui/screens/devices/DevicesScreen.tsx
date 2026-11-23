// Devices of an organization (06-web.md section 7.3, route `/o/:org/devices`, right org:member, edit: admin). The list
// is the verified log's (labels from `device.profile`, kind, assigned workgroups) with what the hub reports: online,
// last seen, agent version, a capacity bar and the capacity-low badge (03-api.md section 4.6.2). The hub list is read
// again whenever the verified log changes (a device enrolled live, an assignment) and every `POLL_MS` for presence –
// only while the tab is visible (a hidden tab spends no requests of the rate limit); it reads again when it is shown.
//
// Admins get the controls: add an org device, assign a platform device (only with platform access, ADR-026 – else the
// reason from the error catalog, `platform_access_denied`, instead of the button), assign an org device to a further
// workgroup, unassign (rotation batch), unpair (rotation batch) and rename. Controls the user's verified rights do not
// allow are not rendered (section 1.2).
import { useEffect, useRef, useState } from 'react';
import { useParams } from 'react-router-dom';
import { formatEventTime } from '../../../core/i18n';
import { actionErrorCode, submitBatch } from '../../../core/org/batch';
import {
  canManageDevices,
  capacityUse,
  DEVICE_PROFILE,
  deviceRows,
  type DeviceRow,
  hasPlatformAccess,
  type HubDevice,
  loadOrgDevices,
  selectDeviceLabels,
  unassignPlan,
  unpairPlan,
} from '../../../core/org/devices';
import { pullOnce } from '../../../core/tasks/pull';
import type { Directory } from '../../../core/tasks/directory';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useNow } from '../../countdown/useNow';
import { ActionDialog, BUTTON, PRIMARY } from '../members/ActionDialog';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { AddDevice } from './AddDevice';
import { AssignPlatformDialog, AssignToWorkgroupDialog, RenameDialog } from './dialogs';
import { deviceName } from './parts';

/** Presence is re-read this often (the hub's online window is 30 s). */
export const POLL_MS = 15_000;

type Open =
  | { readonly kind: 'assign' }
  | { readonly kind: 'to-wg'; readonly row: DeviceRow }
  | { readonly kind: 'rename'; readonly row: DeviceRow }
  | { readonly kind: 'unpair'; readonly row: DeviceRow }
  | { readonly kind: 'unassign'; readonly row: DeviceRow; readonly wg: string };

/** The hub's device list, read again on every log change and every `POLL_MS` while visible (module comment). */
function useHubDevices(org: string, head: string | null): { rows: HubDevice[]; code: string | null } {
  const { app } = useAppEnv();
  const [rows, setRows] = useState<HubDevice[]>([]);
  const [code, setCode] = useState<string | null>(null);
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const visible = () => document.visibilityState === 'visible';
    const timer = setInterval(() => {
      if (visible()) setTick((n) => n + 1);
    }, POLL_MS);
    const shown = () => {
      if (visible()) setTick((n) => n + 1);
    };
    document.addEventListener('visibilitychange', shown);
    return () => {
      clearInterval(timer);
      document.removeEventListener('visibilitychange', shown);
    };
  }, []);
  // answers can arrive out of order (a slow poll after a log change): only the newest request's answer is shown
  const latest = useRef(0);
  useEffect(() => {
    if (head === null) return;
    latest.current += 1;
    const request = latest.current;
    loadOrgDevices(app.tasks, org).then(
      (found) => {
        if (request !== latest.current) return;
        setRows(found);
        setCode(null);
      },
      (failure: unknown) => {
        if (request === latest.current) setCode(actionErrorCode(failure));
      },
    );
  }, [app, org, head, tick]);
  return { rows, code };
}

function CapacityBar({ hub }: { readonly hub: HubDevice }) {
  const { t } = useI18n();
  const use = capacityUse(hub.capacity);
  return (
    <div className="flex flex-col gap-1" data-testid="devices.item.capacity">
      <div
        role="meter"
        aria-valuemin={0}
        aria-valuemax={use.total}
        aria-valuenow={use.used}
        aria-label={t('devices.capacity', { used: use.used, total: use.total })}
        className="h-2 w-full max-w-xs overflow-hidden rounded bg-gray-200"
      >
        <div
          className={hub.capacity_low ? 'h-full bg-red-700' : 'h-full bg-blue-700'}
          style={{ width: `${Math.round(use.share * 100)}%` }}
        />
      </div>
      <span className="text-sm text-gray-700">{t('devices.capacity', { used: use.used, total: use.total })}</span>
    </div>
  );
}

function DeviceItem(props: {
  readonly row: DeviceRow;
  readonly admin: boolean;
  readonly directory: Directory;
  readonly onOpen: (open: Open) => void;
}) {
  const { row, admin, directory, onOpen } = props;
  const { t, locale } = useI18n();
  const now = useNow(false);
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const name = deviceName(t, row);
  const hub = row.hub;
  const wgName = (wg: string) => directory.workgroups.get(wg) ?? t('devices.wg.unnamed');
  return (
    <li className="flex flex-col gap-2 border-b border-gray-200 py-3" data-testid="devices.item" data-device={row.id}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="font-medium" data-testid="devices.item.name">
          {name}
        </span>
        <span className="rounded bg-gray-100 px-2 text-sm" data-testid="devices.item.kind">
          {t(`devices.kind.${row.kind}`)}
        </span>
        <span className="text-sm" data-testid="devices.item.online" data-online={String(hub?.online ?? false)}>
          {hub === null ? t('devices.presence.unknown') : hub.online ? t('devices.online') : t('devices.offline')}
        </span>
        {hub?.capacity_low === true && (
          <span className="rounded bg-red-100 px-2 text-sm text-red-900" data-testid="devices.item.capacity-low">
            {t('devices.capacity-low')}
          </span>
        )}
        {row.decertified && (
          <span className="rounded bg-red-100 px-2 text-sm text-red-900" data-testid="devices.item.decertified">
            {t('devices.decertified')}
          </span>
        )}
      </div>
      {hub !== null && (
        <div className="flex flex-wrap gap-3 text-sm text-gray-700">
          <span data-testid="devices.item.last-seen">
            {hub.last_seen_at === null
              ? t('devices.never-seen')
              : t('devices.last-seen', {
                  time: formatEventTime(Date.parse(hub.last_seen_at), { locale, timeZone, now }),
                })}
          </span>
          <span data-testid="devices.item.version">{t('devices.version', { version: hub.agent_version })}</span>
        </div>
      )}
      {hub !== null && <CapacityBar hub={hub} />}
      <ul className="flex flex-wrap gap-2 text-sm" aria-label={t('devices.workgroups')} data-testid="devices.item.wgs">
        {row.workgroups.length === 0 && <li className="text-gray-600">{t('devices.no-workgroups')}</li>}
        {row.workgroups.map((wg) => (
          <li key={wg} className="flex items-center gap-1 rounded border border-gray-200 px-2" data-wg={wg}>
            <span>{wgName(wg)}</span>
            {admin && (
              <button
                type="button"
                className="min-h-11 min-w-11 underline"
                onClick={() => onOpen({ kind: 'unassign', row, wg })}
                aria-label={t('devices.unassign.label', { name, wg: wgName(wg) })}
                data-testid="devices.item.unassign"
              >
                ×
              </button>
            )}
          </li>
        ))}
      </ul>
      {admin && (
        <div className="flex flex-wrap gap-2">
          {row.kind === 'org' && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => onOpen({ kind: 'to-wg', row })}
              data-testid="devices.item.to-wg"
            >
              {t('devices.to-wg.open')}
            </button>
          )}
          <button
            type="button"
            className={BUTTON}
            onClick={() => onOpen({ kind: 'rename', row })}
            data-testid="devices.item.rename"
          >
            {t('devices.rename.open')}
          </button>
          <button
            type="button"
            className={BUTTON}
            onClick={() => onOpen({ kind: 'unpair', row })}
            data-testid="devices.item.unpair"
          >
            {t('devices.unpair.open')}
          </button>
        </div>
      )}
    </li>
  );
}

/** Where an admin assigns platform devices: the button, or the reason it is missing (ADR-026). */
function PlatformArea({ access, onOpen }: { readonly access: boolean | null; readonly onOpen: () => void }) {
  const { t, error } = useI18n();
  // a non-private org while the platform log is not verified yet: no claim either way
  if (access === null) return null;
  if (!access) {
    const reason = error('platform_access_denied');
    return (
      <div className="rounded border border-gray-300 p-3" role="note" data-testid="devices.platform.denied">
        <p className="font-medium">{reason.title}</p>
        <p>{reason.body}</p>
        <p className="text-sm text-gray-700">{reason.action}</p>
      </div>
    );
  }
  return (
    <button type="button" className={`${PRIMARY} self-start`} onClick={onOpen} data-testid="devices.assign.open">
      {t('devices.assign.open')}
    </button>
  );
}

export function DevicesScreen() {
  const { org } = useParams() as { readonly org: string };
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const labels = useStore(selectDeviceLabels(org), app.store);
  // the verified platform log (grants and certifications re-render the screen)
  const verified = useStore((s) => s.orgs.platform?.state ?? null, app.store);
  const ready = state !== null;
  useEffect(() => {
    if (ready) pullOnce(app.tasks, org, org, { kinds: [DEVICE_PROFILE] }).catch(() => 0);
  }, [app, org, ready]);
  // the platform log is verified by the log job of an org; a fresh session may not have run it yet
  const platformMissing = ready && verified === null;
  useEffect(() => {
    if (platformMissing) app.tasks.refreshLog(org).catch(() => false);
  }, [app, org, platformMissing]);
  const hub = useHubDevices(org, state === null ? null : state.head);
  const [open, setOpen] = useState<Open | null>(null);
  if (state === null)
    return (
      <p className="p-4" data-testid="devices.loading">
        {t('devices.loading')}
      </p>
    );
  if (userId === null || state.member(userId) === null)
    return (
      <p className="p-4" data-testid="devices.not-found">
        {t('devices.not-found')}
      </p>
    );
  const admin = canManageDevices(state, userId);
  const rows = deviceRows(state, verified, hub.rows, labels);
  const close = () => setOpen(null);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="devices.screen">
      <h1 className="text-xl font-semibold">{t('devices.title')}</h1>
      {admin && <AddDevice org={org} state={state} directory={directory} />}
      {admin && (
        <PlatformArea
          access={verified === null && !state.private ? null : hasPlatformAccess(state, verified)}
          onOpen={() => setOpen({ kind: 'assign' })}
        />
      )}
      {hub.code !== null && (
        <p role="alert" className="text-red-800" data-testid="devices.error" data-code={hub.code}>
          {error(hub.code).title}
        </p>
      )}
      {rows.length === 0 && (
        <p className="text-gray-600" data-testid="devices.empty">
          {t('devices.empty')}
        </p>
      )}
      <ul data-testid="devices.list">
        {rows.map((row) => (
          <DeviceItem key={row.id} row={row} admin={admin} directory={directory} onOpen={setOpen} />
        ))}
      </ul>
      {open?.kind === 'assign' && (
        <AssignPlatformDialog org={org} state={state} directory={directory} onClose={close} />
      )}
      {open?.kind === 'to-wg' && (
        <AssignToWorkgroupDialog org={org} row={open.row} state={state} directory={directory} onClose={close} />
      )}
      {open?.kind === 'rename' && <RenameDialog org={org} row={open.row} onClose={close} />}
      {open?.kind === 'unassign' && (
        <ActionDialog
          title={t('devices.unassign.title', {
            name: deviceName(t, open.row),
            wg: directory.workgroups.get(open.wg) ?? t('devices.wg.unnamed'),
          })}
          testId="devices.unassign"
          confirmLabel={t('devices.unassign.confirm')}
          danger
          run={(progress) => submitBatch(app.tasks, org, unassignPlan(open.row.id, open.wg), progress)}
          onDone={close}
          onClose={close}
        >
          <p>{t('devices.unassign.body')}</p>
        </ActionDialog>
      )}
      {open?.kind === 'unpair' && (
        <ActionDialog
          title={t('devices.unpair.title', { name: deviceName(t, open.row) })}
          testId="devices.unpair"
          confirmLabel={t('devices.unpair.confirm')}
          danger
          run={(progress) => submitBatch(app.tasks, org, unpairPlan(open.row.id), progress)}
          onDone={close}
          onClose={close}
        >
          <p>{t('devices.unpair.body')}</p>
        </ActionDialog>
      )}
    </main>
  );
}
