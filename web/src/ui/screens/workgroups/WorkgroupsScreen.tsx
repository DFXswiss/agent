// Workgroups (06-web.md section 7.2, route `/o/:org/workgroups`, right org:member, edit: admin): names (decrypted
// `wg.profile`), my access, devices, archived flag. Admins create a workgroup (one batch: `wg_create`, grants for the
// chosen members, assignments of chosen devices, WK_1 for every reader, `wg.profile`), rename it and archive it (never
// the default workgroup). Controls the user's verified rights do not allow are not rendered (section 1.2).
import { useId, useState } from 'react';
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
import { useAppEnv } from '../../shell/context';
import { ActionDialog, BUTTON, PRIMARY } from '../members/ActionDialog';
import { memberName } from '../members/MembersScreen';
import { NoName } from '../../names/names';
import { useDeviceNames } from '../devices/labels';
import { useDirectory, useOrgView } from '../tasks/hooks';

type Open =
  | { readonly kind: 'create' }
  | { readonly kind: 'rename'; readonly row: WorkgroupRow }
  | { readonly kind: 'archive'; readonly row: WorkgroupRow };

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
  const others = memberRows(state, userId, directory).filter((row) => !row.isMe);
  const choices = assignableDevices(state, platform);
  // devices by their label (`device.profile`, read with the org's profiles), else a sentence – never an ID (ADR-067)
  useDeviceNames([org]); // pulls the org's device labels once
  const labels = useStore(selectDeviceLabels(org), app.store);
  const labelled = (device: string) => labels.has(device);
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
      {others.length > 0 && (
        <fieldset className="flex flex-col gap-2">
          <legend className="font-medium">{t('workgroups.create.members')}</legend>
          {others.map((row) => (
            <label key={row.id} className="flex items-center justify-between gap-2">
              <span>{memberName(t, row)}</span>
              <select
                className="min-h-11 rounded border border-gray-300 px-2"
                value={grants[row.id] ?? ''}
                onChange={(event) => choose(row.id, event.target.value)}
                data-testid={`workgroups.create.member.${row.id}`}
              >
                <option value="">{t('workgroups.access.none')}</option>
                <option value="read">{t('workgroups.access.read')}</option>
                <option value="write">{t('workgroups.access.write')}</option>
              </select>
            </label>
          ))}
        </fieldset>
      )}
      {choices.length > 0 && (
        <fieldset className="flex flex-col gap-2">
          <legend className="font-medium">{t('workgroups.create.devices')}</legend>
          {choices.map((device) => (
            <label key={device.id} className="flex min-h-11 items-center gap-2">
              <input
                type="checkbox"
                checked={devices.includes(device.id)}
                onChange={(event) => toggle(device.id, event.target.checked)}
                data-testid={`workgroups.create.device.${device.id}`}
              />
              <NoName t={t} text={deviceNames(device.id)} id={device.id} showId={!labelled(device.id)} />
            </label>
          ))}
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

function WorkgroupItem({
  org,
  row,
  admin,
  onOpen,
}: {
  readonly org: string;
  readonly row: WorkgroupRow;
  readonly admin: boolean;
  readonly onOpen: (open: Open) => void;
}) {
  const { t } = useI18n();
  const name = row.name ?? t('workgroups.unnamed');
  return (
    <li className="flex flex-col gap-2 border-b border-gray-200 py-3" data-testid="workgroups.item" data-wg={row.id}>
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
      <p className="text-sm text-gray-700">
        <span data-testid="workgroups.item.access">{t(`workgroups.access.${row.myAccess ?? 'none'}`)}</span>
        {' · '}
        <span data-testid="workgroups.item.devices">{t('workgroups.devices', { count: row.devices.length })}</span>
      </p>
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
    </li>
  );
}

export function WorkgroupsScreen() {
  const { org } = useParams() as { readonly org: string };
  const { app } = useAppEnv();
  const { t } = useI18n();
  const { state, userId } = useOrgView(org);
  const directory = useDirectory(org, state !== null);
  const [open, setOpen] = useState<Open | null>(null);
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
  const rows = workgroupRows(state, userId, directory);
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
      <ul data-testid="workgroups.list">
        {rows.map((row) => (
          <WorkgroupItem key={row.id} org={org} row={row} admin={admin} onOpen={setOpen} />
        ))}
      </ul>
      {open?.kind === 'create' && (
        <CreateDialog org={org} state={state} userId={userId} directory={directory} onClose={close} />
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
