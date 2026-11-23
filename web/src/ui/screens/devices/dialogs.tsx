// The device dialogs (06-web.md section 7.3): assign a platform device (workgroup, then the candidates the verified
// platform log certifies; others are shown as not certified and cannot be chosen), assign an org device to a further
// workgroup, and rename. Each runs through the confirmation dialog with the batch progress of the member screen.
import { useEffect, useId, useState } from 'react';
import type { OrgState } from '../../../core/log/state';
import { actionErrorCode, keyRing } from '../../../core/org/batch';
import {
  assignableWorkgroups,
  assignDevice,
  type CandidateRow,
  capacityUse,
  deviceLabel,
  type DeviceRow,
  loadCandidates,
  renameDevice,
} from '../../../core/org/devices';
import type { Directory } from '../../../core/tasks/directory';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { ActionDialog } from '../members/ActionDialog';
import { deviceName } from './parts';

function WorkgroupSelect(props: {
  readonly choices: readonly string[];
  readonly value: string;
  readonly onChange: (wg: string) => void;
  readonly directory: Directory;
  readonly testId: string;
}) {
  const { t } = useI18n();
  const id = useId();
  return (
    <label htmlFor={id} className="flex flex-col gap-1">
      <span>{t('devices.assign.workgroup')}</span>
      <select
        id={id}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={props.value}
        onChange={(event) => props.onChange(event.target.value)}
        data-testid={props.testId}
      >
        {props.choices.map((wg) => (
          <option key={wg} value={wg}>
            {props.directory.workgroups.get(wg) ?? t('devices.wg.unnamed')}
          </option>
        ))}
      </select>
    </label>
  );
}

function Candidates(props: {
  readonly rows: readonly CandidateRow[];
  readonly chosen: string;
  readonly onChoose: (device: string) => void;
}) {
  const { t } = useI18n();
  return (
    <fieldset className="flex flex-col gap-2">
      <legend>{t('devices.assign.candidates')}</legend>
      {props.rows.map((row) => {
        const use = row.capacity === null ? null : capacityUse(row.capacity);
        return (
          <label
            key={row.device_id}
            className="flex min-h-11 flex-wrap items-center gap-2"
            data-testid="devices.assign.candidate"
            data-device={row.device_id}
            data-verified={String(row.verified)}
          >
            <input
              type="radio"
              name="candidate"
              disabled={!row.verified}
              checked={props.chosen === row.device_id}
              onChange={() => props.onChoose(row.device_id)}
            />
            <span>{t('devices.unnamed', { id: row.device_id.slice(-6) })}</span>
            <span>{row.online ? t('devices.online') : t('devices.offline')}</span>
            {use !== null && <span>{t('devices.capacity', { used: use.used, total: use.total })}</span>}
            {row.capacity_low && <span>{t('devices.capacity-low')}</span>}
            {!row.verified && <span className="text-red-800">{t('devices.assign.unverifiable')}</span>}
          </label>
        );
      })}
    </fieldset>
  );
}

/** "Assign platform device": workgroup, verified candidate, the batch with WK/OK and the CK wrap. */
export function AssignPlatformDialog(props: {
  readonly org: string;
  readonly state: OrgState;
  readonly directory: Directory;
  readonly onClose: () => void;
}) {
  const { org, state, directory, onClose } = props;
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const choices = assignableWorkgroups(state, keyRing(app.store, org));
  const [wg, setWg] = useState(choices[0] ?? '');
  // the candidates of the workgroup they were loaded for (a change of the workgroup shows "loading" until its own)
  const [loaded, setLoaded] = useState<{ wg: string; rows: CandidateRow[] | null; code: string | null } | null>(null);
  const [chosen, setChosen] = useState('');
  useEffect(() => {
    if (wg === '') return;
    loadCandidates(app.tasks, org, wg).then(
      (found) => setLoaded({ wg, rows: found, code: null }),
      (failure: unknown) => setLoaded({ wg, rows: null, code: actionErrorCode(failure) }),
    );
  }, [app, org, wg]);
  const current = loaded?.wg === wg ? loaded : null;
  const rows = current?.rows ?? null;
  const code = current?.code ?? null;
  const candidate = rows?.find((row) => row.device_id === chosen && row.verified);
  return (
    <ActionDialog
      title={t('devices.assign.title')}
      testId="devices.assign"
      confirmLabel={t('devices.assign.confirm')}
      ready={candidate !== undefined}
      run={(progress) =>
        assignDevice(
          app.tasks,
          org,
          wg,
          { kind: 'platform', device: candidate!.device_id, platformSeq: candidate!.platform_seq },
          progress,
        )
      }
      onDone={onClose}
      onClose={onClose}
    >
      {choices.length === 0 ? (
        <p data-testid="devices.assign.no-workgroup">{t('devices.assign.no-workgroup')}</p>
      ) : (
        <div className="flex flex-col gap-3">
          <WorkgroupSelect
            choices={choices}
            value={wg}
            onChange={setWg}
            directory={directory}
            testId="devices.assign.wg"
          />
          <p>{t('devices.assign.note')}</p>
          {code !== null && (
            <p role="alert" className="text-red-800" data-testid="devices.assign.load-error" data-code={code}>
              {error(code).title}
            </p>
          )}
          {rows === null && code === null && <p data-testid="devices.assign.loading">{t('devices.assign.loading')}</p>}
          {rows !== null && rows.length === 0 && <p data-testid="devices.assign.none">{t('devices.assign.none')}</p>}
          {rows !== null && rows.length > 0 && <Candidates rows={rows} chosen={chosen} onChoose={setChosen} />}
        </div>
      )}
    </ActionDialog>
  );
}

/** "Assign to workgroup" of an org device (a further `device_assign`, wraps of WK, and CK afterwards). */
export function AssignToWorkgroupDialog(props: {
  readonly org: string;
  readonly row: DeviceRow;
  readonly state: OrgState;
  readonly directory: Directory;
  readonly onClose: () => void;
}) {
  const { org, row, state, directory, onClose } = props;
  const { app } = useAppEnv();
  const { t } = useI18n();
  const choices = assignableWorkgroups(state, keyRing(app.store, org)).filter((wg) => !row.workgroups.includes(wg));
  const [wg, setWg] = useState(choices[0] ?? '');
  return (
    <ActionDialog
      title={t('devices.to-wg.title', { name: deviceName(t, row) })}
      testId="devices.to-wg"
      confirmLabel={t('devices.to-wg.confirm')}
      ready={wg !== ''}
      run={(progress) => assignDevice(app.tasks, org, wg, { kind: 'org', device: row.id }, progress)}
      onDone={onClose}
      onClose={onClose}
    >
      {choices.length === 0 ? (
        <p data-testid="devices.to-wg.none">{t('devices.to-wg.none')}</p>
      ) : (
        <WorkgroupSelect
          choices={choices}
          value={wg}
          onChange={setWg}
          directory={directory}
          testId="devices.to-wg.wg"
        />
      )}
    </ActionDialog>
  );
}

/** Rename (a new `device.profile` event). */
export function RenameDialog(props: { readonly org: string; readonly row: DeviceRow; readonly onClose: () => void }) {
  const { org, row, onClose } = props;
  const { app } = useAppEnv();
  const { t } = useI18n();
  const id = useId();
  const [label, setLabel] = useState(row.label ?? '');
  return (
    <ActionDialog
      title={t('devices.rename.title', { name: deviceName(t, row) })}
      testId="devices.rename"
      confirmLabel={t('devices.rename.confirm')}
      ready={deviceLabel(label) !== null}
      run={() => renameDevice(app.tasks, org, row.id, label)}
      onDone={onClose}
      onClose={onClose}
    >
      <label htmlFor={id} className="flex flex-col gap-1">
        <span>{t('devices.rename.label')}</span>
        <input
          id={id}
          className="min-h-11 rounded border border-gray-300 px-2"
          value={label}
          maxLength={200}
          onChange={(event) => setLabel(event.target.value)}
          data-testid="devices.rename.input"
        />
      </label>
    </ActionDialog>
  );
}
