// The device dialogs (06-web.md section 7.3): assign a platform device (a workgroup or the whole organization, then the
// candidates the verified platform log certifies – for the organization those of the default workgroup –, page by page
// with "Load more", in a searchable picker that renders a bounded number of them; others are shown as not certified
// and cannot be chosen), assign an org device to a further workgroup (every writable workgroup, ADR-058: key holders
// deliver what the admin does not hold) or the whole organization, assign a listed device to the whole organization
// (ADR-075 section 5, with the plain statement of what the device receives), and rename. Each runs through the
// confirmation dialog with the batch progress of the member screen.
import { useCallback, useId, useState } from 'react';
import type { OrgState } from '../../../core/log/state';
import { keyRing } from '../../../core/org/batch';
import {
  assignableWorkgroups,
  assignDevice,
  canAssignToOrg,
  type CandidateRow,
  capacityUse,
  deviceLabel,
  type DeviceRow,
  loadCandidates,
  renameDevice,
  WHOLE_ORG,
} from '../../../core/org/devices';
import type { Directory } from '../../../core/tasks/directory';
import { useI18n } from '../../i18n';
import { Picker } from '../../lists/Picker';
import type { RowProps } from '../../lists/WindowedList';
import { useAppEnv } from '../../shell/context';
import { ActionDialog, BUTTON } from '../members/ActionDialog';
import { usePagedList } from '../members/usePagedList';
import { deviceName } from './parts';
import { NoName } from '../../names/names';

function WorkgroupSelect(props: {
  readonly choices: readonly string[];
  readonly value: string;
  readonly onChange: (wg: string) => void;
  readonly directory: Directory;
  readonly testId: string;
  /** Offer "Whole organization" (value `WHOLE_ORG`) after the workgroups. */
  readonly org?: boolean;
  /** `data-testid` of the label (it leads to the select). */
  readonly labelTestId?: string;
}) {
  const { t } = useI18n();
  const id = useId();
  return (
    <label htmlFor={id} className="flex flex-col gap-1" data-testid={props.labelTestId}>
      <span>{t(props.org === true ? 'devices.assign.scope' : 'devices.assign.workgroup')}</span>
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
        {props.org === true && <option value={WHOLE_ORG}>{t('devices.whole-org')}</option>}
      </select>
    </label>
  );
}

/** What a device given to the whole organization receives (06-web.md section 7.3, said plainly). */
export function OrgNotice() {
  const { t } = useI18n();
  return (
    <p
      className="rounded border border-amber-300 bg-amber-50 p-3 text-amber-950"
      data-testid="devices.assign.org-notice"
    >
      {t('devices.org-notice')}
    </p>
  );
}

const candidateKey = (row: CandidateRow) => row.device_id;

/** A loaded candidate and the radio group's state. */
interface CandidateChoice {
  readonly row: CandidateRow;
  readonly chosen: boolean;
  readonly onChoose: (device: string) => void;
}

const choiceKey = (choice: CandidateChoice) => choice.row.device_id;
const choiceText = (choice: CandidateChoice) => choice.row.device_id;

function CandidateRowView({ item }: RowProps<CandidateChoice>) {
  const { t } = useI18n();
  const { row } = item;
  const use = row.capacity === null ? null : capacityUse(row.capacity);
  return (
    <label
      className="flex min-h-11 flex-wrap items-center gap-2"
      data-testid="devices.assign.candidate"
      data-device={row.device_id}
      data-verified={String(row.verified)}
    >
      <input
        type="radio"
        name="candidate"
        disabled={!row.verified}
        checked={item.chosen}
        onChange={() => item.onChoose(row.device_id)}
      />
      <NoName t={t} text={t('names.device.unknown')} id={row.device_id} showId />
      <span>{row.online ? t('devices.online') : t('devices.offline')}</span>
      {use !== null && <span>{t('devices.capacity', { used: use.used, total: use.total })}</span>}
      {row.capacity_low && <span>{t('devices.capacity-low')}</span>}
      {!row.verified && <span className="text-red-800">{t('devices.assign.unverifiable')}</span>}
    </label>
  );
}

/**
 * The loaded candidates (pages of 100, "Load more" below them) as a radio group in a searchable picker: however many
 * pages are loaded, a bounded number of rows is rendered; the search matches the device ID.
 */
function Candidates(props: {
  readonly rows: readonly CandidateRow[];
  readonly chosen: string;
  readonly onChoose: (device: string) => void;
}) {
  const { t } = useI18n();
  const items = props.rows.map((row): CandidateChoice => ({
    row,
    chosen: props.chosen === row.device_id,
    onChoose: props.onChoose,
  }));
  return (
    <fieldset className="flex flex-col gap-2">
      <legend>{t('devices.assign.candidates')}</legend>
      <Picker
        items={items}
        getKey={choiceKey}
        text={choiceText}
        Row={CandidateRowView}
        testId="devices.assign.candidates"
        label={t('devices.assign.candidates.search')}
      />
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
  const held = keyRing(app.store, org);
  // never empty: the default workgroup is never archived
  const choices = assignableWorkgroups(state);
  const orgChoice = canAssignToOrg(state, held);
  const [wg, setWg] = useState(choices[0]!);
  // the whole organization takes the platform candidates of the default workgroup
  const source = wg === WHOLE_ORG ? state.defaultWg : wg;
  // the candidates of the chosen workgroup, page by page (a change of the workgroup shows "loading" until its own)
  const [chosen, setChosen] = useState('');
  const read = useCallback(
    (before: string | null) => loadCandidates(app.tasks, org, source, before),
    [app, org, source],
  );
  const list = usePagedList(read, candidateKey, source);
  const rows = list.rows;
  const code = list.code;
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
      <div className="flex flex-col gap-3">
        <WorkgroupSelect
          choices={choices}
          value={wg}
          onChange={setWg}
          directory={directory}
          testId="devices.assign.scope"
          labelTestId="devices.assign.wg"
          org={orgChoice}
        />
        {wg === WHOLE_ORG ? <OrgNotice /> : <p>{t('devices.assign.note')}</p>}
        {code !== null && (
          <p role="alert" className="text-red-800" data-testid="devices.assign.load-error" data-code={code}>
            {error(code).title}
          </p>
        )}
        {rows === null && code === null && <p data-testid="devices.assign.loading">{t('devices.assign.loading')}</p>}
        {rows !== null && rows.length === 0 && <p data-testid="devices.assign.none">{t('devices.assign.none')}</p>}
        {rows !== null && rows.length > 0 && <Candidates rows={rows} chosen={chosen} onChoose={setChosen} />}
        {list.truncated && (
          <p className="rounded bg-gray-100 p-3 text-gray-800" role="note" data-testid="devices.assign.truncated">
            {t('devices.assign.truncated')}
          </p>
        )}
        {list.more && (
          <button
            type="button"
            className={`${BUTTON} self-start`}
            disabled={list.paging.loading}
            aria-busy={list.paging.loading}
            onClick={list.paging.loadMore}
            data-testid="devices.assign.load-more"
          >
            {t(list.paging.loading ? 'lists.loading' : 'lists.load-more')}
          </button>
        )}
      </div>
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
  const held = keyRing(app.store, org);
  const choices = assignableWorkgroups(state).filter((wg) => !row.workgroups.includes(wg));
  const orgChoice = canAssignToOrg(state, held);
  const [wg, setWg] = useState(choices[0] ?? (orgChoice ? WHOLE_ORG : ''));
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
      {choices.length === 0 && !orgChoice ? (
        <p data-testid="devices.to-wg.none">{t('devices.to-wg.none')}</p>
      ) : (
        <WorkgroupSelect
          choices={choices}
          value={wg}
          onChange={setWg}
          directory={directory}
          testId="devices.to-wg.wg"
          org={orgChoice}
        />
      )}
      {wg === WHOLE_ORG && <OrgNotice />}
    </ActionDialog>
  );
}

/** "Assign to the whole organization" of a listed device (`device_assign_org`, its `platform_seq` from the log). */
export function AssignOrgDialog(props: {
  readonly org: string;
  readonly row: DeviceRow;
  readonly onClose: () => void;
}) {
  const { org, row, onClose } = props;
  const { app } = useAppEnv();
  const { t } = useI18n();
  const target =
    row.platformSeq === null
      ? ({ kind: 'org', device: row.id } as const)
      : ({ kind: 'platform', device: row.id, platformSeq: row.platformSeq } as const);
  return (
    <ActionDialog
      title={t('devices.assign-org.title', { name: deviceName(t, row) })}
      testId="devices.assign-org"
      confirmLabel={t('devices.assign-org.confirm')}
      run={(progress) => assignDevice(app.tasks, org, WHOLE_ORG, target, progress)}
      onDone={onClose}
      onClose={onClose}
    >
      <OrgNotice />
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
