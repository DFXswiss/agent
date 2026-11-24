// Devices of an organization (06-web.md section 7.3, route `/o/:org/devices`, right org:member, edit: admin). The list
// is the verified log's (labels from `device.profile`, kind, assigned workgroups) with what the hub reports: online,
// last seen, agent version, a capacity bar and the capacity-low badge (03-api.md section 4.6.2). The hub list is read
// again whenever the verified log changes (a device enrolled live, an assignment) and every `POLL_MS` for presence –
// only while the tab is visible (a hidden tab spends no requests of the rate limit); it reads again when it is shown.
//
// A row shows its workgroups or "Whole organization" (ADR-075 section 5, from the verified log), the capacity details
// the runner reports (CPUs, memory, free disk, running VMs, active sessions and the limits) and per VM its workgroup
// and sessions.
//
// Admins get the controls: add an org device, assign a platform device (only with platform access, ADR-026 – else the
// reason from the error catalog, `platform_access_denied`, instead of the button), assign an org device to a further
// workgroup, assign a device to the whole organization and remove it from there (rotation batch), unassign (rotation
// batch), unpair (rotation batch) and rename. Controls the user's verified rights do not
// allow are not rendered (section 1.2).
//
// The rows are a `WindowedList` (ADR-086 section 1: at most `MAX_ROWS` rendered), rebuilt when their inputs change and
// memoized on their content, so a presence poll re-renders only the rows whose presence changed. The hub's list is read
// page by page (pages of 200, newest enrolment first): while older pages exist the rows end with the last device the
// loaded pages name and "Load more" reads the next page (`listedRows`); the log change and the presence poll read the
// whole loaded range again (one request per loaded page), so every shown row has current presence. A capped answer of
// an old hub says so.
import { useCallback, useEffect, useMemo, useState } from 'react';
import { useParams } from 'react-router-dom';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import type { ListPaging } from '../../../core/lists/window';
import { submitBatch } from '../../../core/org/batch';
import {
  canAssignToOrg,
  canManageDevices,
  capacityDetails,
  capacityUse,
  DEVICE_PROFILE,
  deviceRows,
  type DeviceRow,
  hasPlatformAccess,
  type HubDevice,
  listedRows,
  loadOrgDevices,
  selectDeviceLabels,
  sessionsPerVm,
  unassignOrgPlan,
  unassignPlan,
  unpairPlan,
} from '../../../core/org/devices';
import { keyRing } from '../../../core/org/batch';
import { pullOnce } from '../../../core/tasks/pull';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useNow } from '../../countdown/useNow';
import { WindowedList } from '../../lists/WindowedList';
import { ActionDialog, BUTTON, PRIMARY } from '../members/ActionDialog';
import { Chips } from '../members/Chips';
import { MAX_ROWS, PAGE, sameValue } from '../members/lists';
import { PagedNotes } from '../members/PagedNotes';
import { type PagedList, usePagedList } from '../members/usePagedList';
import { useDirectory, useOrgView } from '../tasks/hooks';
import { AddDevice } from './AddDevice';
import { AssignOrgDialog, AssignPlatformDialog, AssignToWorkgroupDialog, RenameDialog } from './dialogs';
import { deviceName } from './parts';

/** Presence is re-read this often (the hub's online window is 30 s). */
export const POLL_MS = 15_000;

export type Open =
  | { readonly kind: 'assign' }
  | { readonly kind: 'to-wg'; readonly row: DeviceRow }
  | { readonly kind: 'rename'; readonly row: DeviceRow }
  | { readonly kind: 'unpair'; readonly row: DeviceRow }
  | { readonly kind: 'assign-org'; readonly row: DeviceRow }
  | { readonly kind: 'unassign-org'; readonly row: DeviceRow }
  | { readonly kind: 'unassign'; readonly row: DeviceRow; readonly wg: string };

const hubKey = (row: HubDevice) => row.device_id;

/**
 * The hub's device list page by page (`usePagedList`, pages of 200), read again on every log change and every
 * `POLL_MS` while visible (module comment): a re-read covers the pages loaded so far, so "Load more" keeps the presence
 * of the older devices current as well.
 */
function useHubDevices(org: string, head: string | null): PagedList<HubDevice> {
  const { app } = useAppEnv();
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
  const read = useCallback((before: string | null) => loadOrgDevices(app.tasks, org, before), [app, org]);
  return usePagedList(read, hubKey, `${head}/${tick}`, head !== null);
}

const NO_HUB: readonly HubDevice[] = [];

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

/** The capacity details the runner reports (ADR-075 section 2); memory and disk in GiB. */
function CapacityDetails({ hub }: { readonly hub: HubDevice }) {
  const { t, locale } = useI18n();
  const gib = new Intl.NumberFormat(locale, { maximumFractionDigits: 1 });
  const details = capacityDetails(hub.capacity);
  if (details.length === 0) return null;
  return (
    <ul className="flex flex-wrap gap-x-3 text-sm text-gray-700" data-testid="devices.item.details">
      {details.map(([field, value]) => {
        const text =
          (field === 'sessions_per_vm' || field === 'vms_max') && value === 0
            ? t(`devices.detail.${field}.none`)
            : t(`devices.detail.${field}`, {
                value: field.endsWith('_mib') ? gib.format(value / 1024) : String(value),
              });
        return (
          <li key={field} data-detail={field}>
            {text}
          </li>
        );
      })}
    </ul>
  );
}

/** Per VM of the device: its workgroup and sessions ("vm-… · Research · 3 of 16 sessions"). */
function VmList({ hub, wgName }: { readonly hub: HubDevice; readonly wgName: (wg: string) => string }) {
  const { t } = useI18n();
  const vms = hub.vms ?? [];
  if (vms.length === 0) return null;
  const limit = sessionsPerVm(hub.capacity);
  return (
    <ul className="flex flex-col text-sm text-gray-700" aria-label={t('devices.vms')}>
      {vms.map((vm) => (
        <li key={vm.vm_id} data-testid="devices.vm" data-vm={vm.vm_id} data-wg={vm.wg_id} data-sessions={vm.sessions}>
          {[
            vm.vm_id,
            wgName(vm.wg_id),
            limit === null
              ? t('devices.vm.sessions', { sessions: vm.sessions })
              : t('devices.vm.sessions-of', { sessions: vm.sessions, limit }),
          ].join(' · ')}
        </li>
      ))}
    </ul>
  );
}

/** What a device row shows: the row, the viewer's rights, the workgroup names (one selector for the list). */
interface DeviceEntry {
  readonly row: DeviceRow;
  readonly admin: boolean;
  /** The user may give devices to the whole organization (admin holding the current `OK`). */
  readonly toOrg: boolean;
  readonly names: ReadonlyMap<string, string>;
  readonly onOpen: (open: Open) => void;
}

function DeviceRowView({ item }: { readonly item: DeviceEntry }) {
  const { row, admin, toOrg, names, onOpen } = item;
  const { t, locale } = useI18n();
  const now = useNow(false);
  const name = deviceName(t, row);
  const hub = row.hub;
  const wgName = (wg: string) => names.get(wg) ?? t('devices.wg.unnamed');
  return (
    <>
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
                  time: formatEventTime(Date.parse(hub.last_seen_at), { locale, timeZone: localTimeZone(), now }),
                })}
          </span>
          <span data-testid="devices.item.version">{t('devices.version', { version: hub.agent_version })}</span>
        </div>
      )}
      {hub !== null && <CapacityBar hub={hub} />}
      {hub !== null && <CapacityDetails hub={hub} />}
      {hub !== null && <VmList hub={hub} wgName={wgName} />}
      <Chips
        items={row.explicit}
        wgOf={sameWg}
        testId="devices.item.wgs"
        label={t('devices.workgroups')}
        more="devices.wgs.more"
        {...(row.workgroups.length === 0 && {
          empty: <li className="text-gray-600">{t('devices.no-workgroups')}</li>,
        })}
        {...(row.org && {
          lead: (
            <li className="rounded border border-gray-200 px-2 font-medium" data-testid="devices.item.org">
              {t('devices.whole-org')}
            </li>
          ),
        })}
        render={(wg) => (
          <>
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
          </>
        )}
      />
      {admin && (
        <div className="flex flex-wrap gap-2">
          {row.kind === 'org' && !row.org && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => onOpen({ kind: 'to-wg', row })}
              data-testid="devices.item.to-wg"
            >
              {t('devices.to-wg.open')}
            </button>
          )}
          {!row.org && toOrg && !row.decertified && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => onOpen({ kind: 'assign-org', row })}
              data-testid="devices.item.assign-org"
            >
              {t('devices.assign-org.open')}
            </button>
          )}
          {row.org && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => onOpen({ kind: 'unassign-org', row })}
              data-testid="devices.unassign-org"
            >
              {t('devices.unassign-org.open')}
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
    </>
  );
}

const sameWg = (wg: string) => wg;
const deviceKey = (entry: DeviceEntry) => entry.row.id;
const deviceAttributes = (entry: DeviceEntry) => ({
  'data-testid': 'devices.item',
  'data-device': entry.row.id,
  'data-assigned-org': String(entry.row.org),
});

/** The device rows, newest member first, windowed; a presence poll re-renders only rows whose content changed. */
export function DeviceList(props: {
  readonly rows: readonly DeviceRow[];
  readonly admin: boolean;
  readonly names: ReadonlyMap<string, string>;
  /** The user may give devices to the whole organization (admin holding the current `OK`). */
  readonly toOrg: boolean;
  readonly onOpen: (open: Open) => void;
  /** "Load more" of the hub's next page, and the number of all devices while only some are shown. */
  readonly paging?: ListPaging;
  readonly total?: number;
}) {
  const { rows, admin, names, toOrg, onOpen, paging, total } = props;
  const entries = useMemo(
    () => rows.map((row): DeviceEntry => ({ row, admin, toOrg, names, onOpen })),
    [rows, admin, toOrg, names, onOpen],
  );
  return (
    <WindowedList
      listId="devices"
      items={entries}
      getKey={deviceKey}
      Row={DeviceRowView}
      same={sameValue}
      newest="start"
      page={PAGE}
      maxRows={MAX_ROWS}
      testId="devices.list"
      labels={{ older: 'devices.list.more', newer: 'devices.list.above', newest: 'devices.list.top' }}
      rowClassName="flex flex-col gap-2 border-b border-gray-200 py-3"
      rowAttributes={deviceAttributes}
      rowHeight="lg"
      {...(paging !== undefined && { hasMore: paging.hasMore, loading: paging.loading, onLoadMore: paging.loadMore })}
      {...(total !== undefined && { total })}
    />
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
  // rebuilt when the log, the platform log, the hub's list or the labels change, not on every render
  const hubRows = hub.rows ?? NO_HUB;
  const all = useMemo(
    () => (state === null ? [] : deviceRows(state, verified, hubRows, labels)),
    [state, verified, hubRows, labels],
  );
  const rows = useMemo(() => listedRows(all, hub.more), [all, hub.more]);
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
  const toOrg = admin && canAssignToOrg(state, keyRing(app.store, org));
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
      {all.length === 0 && (
        <p className="text-gray-600" data-testid="devices.empty">
          {t('devices.empty')}
        </p>
      )}
      <PagedNotes list={hub} shown={rows.length} note="devices.truncated" rows="devices.list" testId="devices" />
      <DeviceList
        rows={rows}
        admin={admin}
        names={directory.workgroups}
        toOrg={toOrg}
        onOpen={setOpen}
        paging={hub.paging}
        {...(hub.more && { total: all.length })}
      />
      {open?.kind === 'assign' && (
        <AssignPlatformDialog org={org} state={state} directory={directory} onClose={close} />
      )}
      {open?.kind === 'to-wg' && (
        <AssignToWorkgroupDialog org={org} row={open.row} state={state} directory={directory} onClose={close} />
      )}
      {open?.kind === 'rename' && <RenameDialog org={org} row={open.row} onClose={close} />}
      {open?.kind === 'assign-org' && <AssignOrgDialog org={org} row={open.row} onClose={close} />}
      {open?.kind === 'unassign-org' && (
        <ActionDialog
          title={t('devices.unassign-org.title', { name: deviceName(t, open.row) })}
          testId="devices.unassign-org"
          confirmLabel={t('devices.unassign-org.confirm')}
          danger
          run={(progress) => submitBatch(app.tasks, org, unassignOrgPlan(open.row.id), progress)}
          onDone={close}
          onClose={close}
        >
          <p>{t('devices.unassign-org.body')}</p>
        </ActionDialog>
      )}
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
