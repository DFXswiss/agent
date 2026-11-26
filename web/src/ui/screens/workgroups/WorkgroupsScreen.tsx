// Workgroups (06-web.md section 7.2, route `/o/:org/workgroups`, right org:member, edit: admin): names (decrypted
// `wg.profile`), my access, devices, archived flag. Admins create a workgroup (one batch: `wg_create`, grants for the
// chosen members, assignments of chosen devices, WK_1 for every reader, `wg.profile`), rename it and archive it (never
// the default workgroup). Writers of a workgroup set its default device (and VM) for new tasks, shown in its row
// (`PUT /v1/workgroups/{wg}/placement`, 06-web.md section 7.3, ADR-075 section 6). Controls the user's verified rights
// do not allow are not rendered (section 1.2). The rows are a `WindowedList` (ADR-086 section 1: at most `MAX_ROWS`
// rendered), built once per log change; the members and devices of the create dialog are searchable pickers that
// render a bounded number of choices (`lists/Picker.tsx`). A VM of the workgroup that its device reports outdated
// shows why it waits (or that it moves when its sessions are paused) and offers "Switch now" with a preview of the
// paused sessions that move along (`POST /v1/workgroups/{wg}/vms/{vm}/switch`, ADR-093; the hub refuses members without
// administration rights).
import { type ReactNode, useId, useMemo, useState } from 'react';
import { actionErrorCode } from '../../../core/org/batch';
import {
  type Placement,
  setWorkgroupPlacement,
  validChoice,
  type WorkgroupDevices,
} from '../../../core/tasks/placement';
import {
  type OutdatedVm,
  outdatedVms,
  requestVmSwitch,
  shortId,
  switchStatus,
  withoutState,
} from '../../../core/tasks/vmswitch';
import { formatEventTime, localTimeZone } from '../../../core/i18n';
import { useNow } from '../../countdown/useNow';
import { PlacementSelect, useWorkgroupDevices } from '../../composer/DeviceChoice';
import { Dialog } from '../members/Dialog';
import { Link, useParams } from 'react-router-dom';
import { uuid7 } from '../../../core/encoding/ids';
import { submitBatch } from '../../../core/org/batch';
import { selectDeviceLabels } from '../../../core/org/devices';
import { type Access, canManage, memberRows } from '../../../core/org/members';
import {
  archivePlan,
  assignableDevices,
  createWorkgroupPlan,
  renameWorkgroup,
  workgroupName,
  type WorkgroupRow,
  workgroupRows,
} from '../../../core/org/workgroups';
import type { OrgState } from '../../../core/log/state';
import type { Directory } from '../../../core/tasks/directory';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { Picker } from '../../lists/Picker';
import { type RowProps, WindowedList } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { ActionDialog, BUTTON, PRIMARY } from '../members/ActionDialog';
import { MAX_ROWS, PAGE, sameValue } from '../members/lists';
import { memberName } from '../members/MembersScreen';
import { NoName } from '../../names/names';
import { useDeviceNames } from '../devices/labels';
import { useDirectory, useOrgView } from '../tasks/hooks';

export type Open =
  | { readonly kind: 'create' }
  | { readonly kind: 'placement'; readonly row: WorkgroupRow }
  | { readonly kind: 'rename'; readonly row: WorkgroupRow }
  | { readonly kind: 'archive'; readonly row: WorkgroupRow }
  | { readonly kind: 'vm-switch'; readonly row: WorkgroupRow; readonly vm: OutdatedVm };

/** A member of the create dialog with the access chosen for it. */
interface MemberChoice {
  readonly id: string;
  readonly text: string;
  readonly access: string;
  readonly choose: (user: string, value: string) => void;
}

/** A device of the create dialog and whether it is chosen. */
interface DeviceChoice {
  readonly id: string;
  readonly text: string;
  /** Whether the device has a label (else its ID is shown below the sentence). */
  readonly labelled: boolean;
  readonly checked: boolean;
  readonly toggle: (device: string, on: boolean) => void;
}

const choiceKey = (choice: { readonly id: string }) => choice.id;
const choiceText = (choice: { readonly text: string }) => choice.text;

function MemberChoiceRow({ item }: RowProps<MemberChoice>) {
  const { t } = useI18n();
  return (
    <label className="flex items-center justify-between gap-2">
      <span>{item.text}</span>
      <select
        className="min-h-11 rounded border border-gray-300 px-2"
        value={item.access}
        onChange={(event) => item.choose(item.id, event.target.value)}
        data-testid={`workgroups.create.member.${item.id}`}
      >
        <option value="">{t('workgroups.access.none')}</option>
        <option value="read">{t('workgroups.access.read')}</option>
        <option value="write">{t('workgroups.access.write')}</option>
      </select>
    </label>
  );
}

function DeviceChoiceRow({ item }: RowProps<DeviceChoice>) {
  const { t } = useI18n();
  return (
    <label className="flex min-h-11 items-center gap-2">
      <input
        type="checkbox"
        checked={item.checked}
        onChange={(event) => item.toggle(item.id, event.target.checked)}
        data-testid={`workgroups.create.device.${item.id}`}
      />
      <NoName t={t} text={item.text} id={item.id} showId={!item.labelled} />
    </label>
  );
}

interface CreateProps {
  readonly org: string;
  readonly state: OrgState;
  readonly userId: string;
  readonly directory: Directory;
  readonly onClose: () => void;
}

function CreateDialog({ org, state, userId, directory, onClose }: CreateProps) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const nameId = useId();
  const platform = useStore((s) => s.orgs.platform?.state ?? null, app.store);
  // the ID is drawn once: a retry creates the same workgroup (and is "done" if the hub took the first attempt)
  const [wg] = useState(() => uuid7());
  const [name, setName] = useState('');
  const [grants, setGrants] = useState<Record<string, Access>>({});
  const [devices, setDevices] = useState<string[]>([]);
  const others = useMemo(
    () => memberRows(state, userId, directory).filter((row) => !row.isMe),
    [state, userId, directory],
  );
  const choices = useMemo(() => assignableDevices(state, platform), [state, platform]);
  // devices by their label (`device.profile`, read with the org's profiles), else a sentence – never an ID (ADR-073)
  useDeviceNames([org]); // pulls the org's device labels once
  const labels = useStore(selectDeviceLabels(org), app.store);
  const deviceNames = (device: string) =>
    labels.get(device) ??
    t(state.member(device)?.platformSeq == null ? 'names.device.unknown' : 'names.device.platform.unknown');
  const choose = (user: string, value: string) => {
    const next = { ...grants };
    if (value === '') delete next[user];
    else next[user] = value as Access;
    setGrants(next);
  };
  const toggle = (device: string, on: boolean) =>
    setDevices(on ? [...devices, device] : devices.filter((item) => item !== device));
  // the choices as picker items (searched by name; a filtered-out choice keeps its value)
  const members = others.map((row): MemberChoice => ({
    id: row.id,
    text: memberName(t, row),
    access: grants[row.id] ?? '',
    choose,
  }));
  const deviceChoices = choices.map((device): DeviceChoice => ({
    id: device.id,
    text: deviceNames(device.id),
    labelled: labels.has(device.id),
    checked: devices.includes(device.id),
    toggle,
  }));
  return (
    <ActionDialog
      title={t('workgroups.create.title')}
      testId="workgroups.create"
      confirmLabel={t('workgroups.create.confirm')}
      ready={workgroupName(name) !== null}
      run={(progress) =>
        submitBatch(app.tasks, org, createWorkgroupPlan({ wg, name, grants, devices }, app.tasks.now), progress)
      }
      onDone={onClose}
      onClose={onClose}
    >
      <label htmlFor={nameId} className="font-medium">
        {t('workgroups.name')}
      </label>
      <input
        id={nameId}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={name}
        maxLength={200}
        onChange={(event) => setName(event.target.value)}
        data-testid="workgroups.create.name"
      />
      <p className="text-sm text-gray-700">{t('workgroups.create.note')}</p>
      {members.length > 0 && (
        <fieldset className="flex flex-col gap-2">
          <legend className="font-medium">{t('workgroups.create.members')}</legend>
          <Picker
            items={members}
            getKey={choiceKey}
            text={choiceText}
            Row={MemberChoiceRow}
            testId="workgroups.create.members"
            label={t('workgroups.create.members.search')}
          />
        </fieldset>
      )}
      {deviceChoices.length > 0 && (
        <fieldset className="flex flex-col gap-2">
          <legend className="font-medium">{t('workgroups.create.devices')}</legend>
          <Picker
            items={deviceChoices}
            getKey={choiceKey}
            text={choiceText}
            Row={DeviceChoiceRow}
            testId="workgroups.create.devices"
            label={t('workgroups.create.devices.search')}
          />
        </fieldset>
      )}
    </ActionDialog>
  );
}

function RenameDialog({
  org,
  row,
  current,
  onClose,
}: {
  readonly org: string;
  readonly row: WorkgroupRow;
  readonly current: string;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const nameId = useId();
  const [name, setName] = useState(current);
  return (
    <ActionDialog
      title={t('workgroups.rename.title')}
      testId="workgroups.rename"
      confirmLabel={t('workgroups.rename.confirm')}
      ready={workgroupName(name) !== null}
      run={() => renameWorkgroup(app.tasks, org, row.id, name)}
      onDone={onClose}
      onClose={onClose}
    >
      <label htmlFor={nameId} className="font-medium">
        {t('workgroups.name')}
      </label>
      <input
        id={nameId}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={name}
        maxLength={200}
        onChange={(event) => setName(event.target.value)}
        data-testid="workgroups.rename.name"
      />
    </ActionDialog>
  );
}

/** The default device of a workgroup: "Automatic" or a device and optionally a VM. */
function PlacementDialog(props: {
  readonly org: string;
  readonly row: WorkgroupRow;
  readonly onSaved: () => void;
  readonly onClose: () => void;
}) {
  const { org, row, onSaved, onClose } = props;
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const devices = useWorkgroupDevices(row.id);
  const [picked, setPicked] = useState<{ value: Placement | null } | null>(null);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const value = picked?.value ?? (devices === null ? null : validChoice(devices.devices, devices.placement));
  const save = async () => {
    setBusy(true);
    setCode(null);
    try {
      await setWorkgroupPlacement(app.tasks, row.id, value);
      onSaved();
    } catch (failure) {
      setCode(actionErrorCode(failure));
      setBusy(false);
    }
  };
  return (
    <Dialog title={t('workgroups.placement.title')} testId="workgroups.placement" busy={busy} onClose={onClose}>
      <p className="text-sm text-gray-700">{t('workgroups.placement.note')}</p>
      {devices === null ? (
        <p data-testid="workgroups.placement.loading">{t('workgroups.placement.loading')}</p>
      ) : (
        <PlacementSelect
          org={org}
          devices={devices}
          value={value}
          onChange={(next) => setPicked({ value: next })}
          testId="workgroups.placement"
          disabled={busy}
        />
      )}
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="workgroups.placement.error" data-code={code}>
          {error(code).title}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          className={PRIMARY}
          disabled={busy || devices === null}
          onClick={() => void save()}
          data-testid="workgroups.placement.save"
        >
          {t('workgroups.placement.save')}
        </button>
        <button type="button" className={BUTTON} disabled={busy} onClick={onClose}>
          {t('workgroups.placement.cancel')}
        </button>
      </div>
    </Dialog>
  );
}

/** The row's default device ("Default device: Automatic" or "… <device> · vm-…"), read from the hub. */
function PlacementLine({ org, devices }: { readonly org: string; readonly devices: WorkgroupDevices }) {
  const { t } = useI18n();
  const name = useDeviceNames([org]);
  const placement = validChoice(devices.devices, devices.placement);
  const text =
    placement === null
      ? t('workgroups.placement.auto')
      : [name(placement.device_id), ...(placement.vm_id === null ? [] : [placement.vm_id])].join(' · ');
  return (
    <span data-testid="workgroups.item.placement" data-device={placement?.device_id ?? ''}>
      {t('workgroups.placement.line', { device: text })}
    </span>
  );
}

/** In the confirmation of "Switch now": the sessions that continue without the state of the old VM (HOT-24). */
function WithoutStateList({ upgrade }: { readonly upgrade: OutdatedVm['upgrade'] }) {
  const { t, locale } = useI18n();
  const now = useNow(false);
  const items = withoutState(upgrade);
  if (items.length === 0) return null;
  const time = (text: string) => formatEventTime(Date.parse(text), { locale, timeZone: localTimeZone(), now });
  return (
    <div className="text-amber-800" data-testid="workgroups.vm-switch.dialog.without">
      <p>{t('workgroups.vm-switch.without')}</p>
      <ul className="list-disc pl-5">
        {items.map((item) => (
          <li key={item.session_id} data-testid="workgroups.vm-switch.dialog.without.item">
            {item.checkpoint_at === null
              ? t('workgroups.vm-switch.without.fresh', { session: shortId(item.session_id) })
              : t('workgroups.vm-switch.without.saved', {
                  session: shortId(item.session_id),
                  time: time(item.checkpoint_at),
                })}
          </li>
        ))}
      </ul>
    </div>
  );
}

/** The outdated VMs of a workgroup: why each waits, and "Switch now" (ADR-093). */
function VmSwitchList(props: {
  readonly row: WorkgroupRow;
  readonly devices: WorkgroupDevices;
  readonly requested: Readonly<Record<string, string>>;
  readonly onOpen: (open: Open) => void;
}) {
  const { row, devices, requested, onOpen } = props;
  const { t, locale } = useI18n();
  const now = useNow(false);
  const time = (text: string) => formatEventTime(Date.parse(text), { locale, timeZone: localTimeZone(), now });
  const vms = outdatedVms(devices.devices);
  if (vms.length === 0) return null;
  return (
    <ul className="flex flex-col gap-2 text-sm" data-testid="workgroups.vm-switch">
      {vms.map((vm) => {
        const status = switchStatus(vm.upgrade);
        const at = requested[vm.vm_id];
        return (
          <li
            key={vm.vm_id}
            className="flex flex-col gap-1"
            data-testid="workgroups.vm-switch.item"
            data-vm={vm.vm_id}
            data-status={status}
          >
            <span>{t('workgroups.vm-switch.line', { vm: vm.vm_id, golden: vm.upgrade.golden_version })}</span>
            {status === 'moves' && <span>{t('workgroups.vm-switch.moves')}</span>}
            {status === 'switching' && <span>{t('workgroups.vm-switch.switching')}</span>}
            {status === 'held' &&
              vm.upgrade.holds.map((hold) => (
                <span
                  key={`${hold.reason}.${hold.session_id ?? ''}`}
                  className="text-amber-800"
                  data-testid="workgroups.vm-switch.hold"
                  data-reason={hold.reason}
                >
                  {t(`workgroups.vm-switch.hold.${hold.reason}`, {
                    session:
                      hold.session_id === null ? t('workgroups.vm-switch.session.none') : shortId(hold.session_id),
                    time: time(hold.since),
                  })}
                </span>
              ))}
            {at !== undefined && (
              <span data-testid="workgroups.vm-switch.requested">
                {t('workgroups.vm-switch.requested', { time: time(at) })}
              </span>
            )}
            {status !== 'switching' && (
              <button
                type="button"
                className={`${BUTTON} self-start`}
                onClick={() => onOpen({ kind: 'vm-switch', row, vm })}
                data-testid="workgroups.vm-switch.open"
              >
                {t('workgroups.vm-switch.open')}
              </button>
            )}
          </li>
        );
      })}
    </ul>
  );
}

/** The facts of a row the viewer can read: access, devices, default device and the outdated VMs (one hub read). */
function AccessFacts({ item }: { readonly item: WorkgroupEntry }) {
  const { org, row, version, requested, onOpen } = item;
  const devices = useWorkgroupDevices(row.id, version);
  return (
    <>
      <Facts row={row}>
        {devices !== null && (
          <>
            {' · '}
            <PlacementLine org={org} devices={devices} />
          </>
        )}
      </Facts>
      {devices !== null && <VmSwitchList row={row} devices={devices} requested={requested} onOpen={onOpen} />}
    </>
  );
}

function Facts({ row, children }: { readonly row: WorkgroupRow; readonly children?: ReactNode }) {
  const { t } = useI18n();
  return (
    <p className="text-sm text-gray-700">
      <span data-testid="workgroups.item.access">{t(`workgroups.access.${row.myAccess ?? 'none'}`)}</span>
      {' · '}
      <span data-testid="workgroups.item.devices">{t('workgroups.devices', { count: row.devices.length })}</span>
      {children}
    </p>
  );
}

/** What a workgroup row shows: the row, the org (for the link) and the viewer's rights. */
interface WorkgroupEntry {
  readonly org: string;
  readonly row: WorkgroupRow;
  readonly admin: boolean;
  /** `wg:write`: the user sets the workgroup's default device. */
  readonly writer: boolean;
  /** Changes after a saved default (the row reads it again). */
  readonly version: number;
  /** When a switch of a VM was requested here (VM ID -> time). */
  readonly requested: Readonly<Record<string, string>>;
  readonly onOpen: (open: Open) => void;
}

function WorkgroupRowView({ item }: { readonly item: WorkgroupEntry }) {
  const { org, row, admin, writer, onOpen } = item;
  const { t } = useI18n();
  const name = row.name ?? t('workgroups.unnamed');
  return (
    <>
      <div className="flex flex-wrap items-center gap-2">
        {row.myAccess === null ? (
          <span className="font-medium" data-testid="workgroups.item.name">
            {name}
          </span>
        ) : (
          <Link
            className="inline-flex min-h-11 items-center font-medium underline"
            to={`/o/${encodeURIComponent(org)}/w/${encodeURIComponent(row.id)}`}
            data-testid="workgroups.item.name"
          >
            {name}
          </Link>
        )}
        {row.isDefault && <span className="rounded bg-gray-100 px-2 text-sm">{t('workgroups.default')}</span>}
        {row.archived && (
          <span className="rounded bg-gray-100 px-2 text-sm" data-testid="workgroups.item.archived">
            {t('workgroups.archived')}
          </span>
        )}
      </div>
      {row.myAccess === null ? <Facts row={row} /> : <AccessFacts item={item} />}
      {writer && (
        <button
          type="button"
          className={`${BUTTON} self-start`}
          onClick={() => onOpen({ kind: 'placement', row })}
          data-testid="workgroups.placement.open"
          data-wg={row.id}
        >
          {t('workgroups.placement.open')}
        </button>
      )}
      {admin && !row.archived && (
        <div className="flex flex-wrap gap-2">
          <button
            type="button"
            className={BUTTON}
            onClick={() => onOpen({ kind: 'rename', row })}
            data-testid="workgroups.item.rename"
          >
            {t('workgroups.rename.open')}
          </button>
          {!row.isDefault && (
            <button
              type="button"
              className={BUTTON}
              onClick={() => onOpen({ kind: 'archive', row })}
              data-testid="workgroups.item.archive"
            >
              {t('workgroups.archive.open')}
            </button>
          )}
        </div>
      )}
    </>
  );
}

const workgroupKey = (entry: WorkgroupEntry) => entry.row.id;
const workgroupAttributes = (entry: WorkgroupEntry) => ({ 'data-testid': 'workgroups.item', 'data-wg': entry.row.id });

const NONE_REQUESTED: Readonly<Record<string, string>> = {};

/** The workgroup rows in the order of `workgroupRows` (default first, archived last), windowed. */
export function WorkgroupList(props: {
  readonly org: string;
  readonly rows: readonly WorkgroupRow[];
  readonly admin: boolean;
  /** `wg:write` of the user (sets a workgroup's default device). */
  readonly writable: (wg: string) => boolean;
  /** Changes after a saved default (the rows read it again). */
  readonly version: number;
  readonly requested?: Readonly<Record<string, string>>;
  readonly onOpen: (open: Open) => void;
}) {
  const { org, rows, admin, writable, version, requested = NONE_REQUESTED, onOpen } = props;
  const entries = useMemo(
    () =>
      rows.map((row): WorkgroupEntry => ({ org, row, admin, writer: writable(row.id), version, requested, onOpen })),
    [org, rows, admin, writable, version, requested, onOpen],
  );
  return (
    <WindowedList
      listId="workgroups"
      items={entries}
      getKey={workgroupKey}
      Row={WorkgroupRowView}
      same={sameValue}
      newest="start"
      page={PAGE}
      maxRows={MAX_ROWS}
      testId="workgroups.list"
      labels={{ older: 'workgroups.list.more', newer: 'workgroups.list.above', newest: 'workgroups.list.top' }}
      rowClassName="flex flex-col gap-2 border-b border-gray-200 py-3"
      rowAttributes={workgroupAttributes}
    />
  );
}

export function WorkgroupsScreen() {
  const { org } = useParams() as { readonly org: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const [open, setOpen] = useState<Open | null>(null);
  const [version, setVersion] = useState(0);
  const [requested, setRequested] = useState<Readonly<Record<string, string>>>(NONE_REQUESTED);
  // rebuilt only when the verified log, the user or the names change, not on every render
  const rows = useMemo(
    () => (state === null || userId === null ? [] : workgroupRows(state, userId, directory)),
    [state, userId, directory],
  );
  if (state === null) {
    return (
      <p className="p-4" data-testid="workgroups.loading">
        {t('workgroups.loading')}
      </p>
    );
  }
  if (userId === null || state.member(userId) === null) {
    return (
      <p className="p-4" data-testid="workgroups.not-found">
        {t('workgroups.not-found')}
      </p>
    );
  }
  const admin = canManage(state, userId);
  const close = () => setOpen(null);
  return (
    <main className="mx-auto flex w-full max-w-3xl flex-col gap-4 p-4" data-testid="workgroups.screen">
      <h1 className="text-xl font-semibold">{t('workgroups.title')}</h1>
      {admin && (
        <button
          type="button"
          className={`${PRIMARY} self-start`}
          onClick={() => setOpen({ kind: 'create' })}
          data-testid="workgroups.create.open"
        >
          {t('workgroups.create.open')}
        </button>
      )}
      <WorkgroupList
        org={org}
        rows={rows}
        admin={admin}
        writable={(wg) => state.canWriteWg(userId, wg)}
        version={version}
        requested={requested}
        onOpen={setOpen}
      />
      {open?.kind === 'create' && (
        <CreateDialog org={org} state={state} userId={userId} directory={directory} onClose={close} />
      )}
      {open?.kind === 'placement' && (
        <PlacementDialog
          org={org}
          row={open.row}
          onSaved={() => {
            setVersion((n) => n + 1);
            close();
          }}
          onClose={close}
        />
      )}
      {open?.kind === 'vm-switch' && (
        <ActionDialog
          title={t('workgroups.vm-switch.title', { vm: open.vm.vm_id })}
          testId="workgroups.vm-switch.dialog"
          confirmLabel={t('workgroups.vm-switch.confirm')}
          run={async () => {
            const answer = await requestVmSwitch(app.tasks, open.row.id, open.vm.vm_id);
            if (answer.requested_at !== null) {
              const at = answer.requested_at;
              setRequested((before) => ({ ...before, [open.vm.vm_id]: at }));
            }
          }}
          onDone={() => {
            setVersion((n) => n + 1);
            close();
          }}
          onClose={close}
        >
          <p>{t('workgroups.vm-switch.body')}</p>
          <p data-testid="workgroups.vm-switch.dialog.sessions">
            {open.vm.upgrade.sessions.length === 0
              ? t('workgroups.vm-switch.sessions.none')
              : t('workgroups.vm-switch.sessions', {
                  count: open.vm.upgrade.sessions.length,
                  ids: open.vm.upgrade.sessions.map(shortId).join(', '),
                })}
          </p>
          <WithoutStateList upgrade={open.vm.upgrade} />
        </ActionDialog>
      )}
      {open?.kind === 'rename' && (
        <RenameDialog org={org} row={open.row} current={open.row.name ?? ''} onClose={close} />
      )}
      {open?.kind === 'archive' && (
        <ActionDialog
          title={t('workgroups.archive.title', { name: open.row.name ?? t('workgroups.unnamed') })}
          testId="workgroups.archive"
          confirmLabel={t('workgroups.archive.confirm')}
          danger
          run={(progress) => submitBatch(app.tasks, org, archivePlan(open.row.id), progress)}
          onDone={close}
          onClose={close}
        >
          <p>{t('workgroups.archive.body')}</p>
        </ActionDialog>
      )}
    </main>
  );
}
