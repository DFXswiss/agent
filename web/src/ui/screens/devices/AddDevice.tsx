// "Add org device" (06-web.md section 7.3, 04-crypto.md sections 5.7, 5.8): the admin chooses the workgroups of the
// device (only workgroups whose current key the admin holds; the default workgroup preselected) and the expiry (24 h
// default, 1 h – 30 d), and gets the code once with the command `agent-runner enroll --hub <origin> --code <code>`
// (copy buttons, confidentiality warning). The result follows the verified log: once the device's `accept` arrives (the
// live channel's `log` frame refreshes the log) it says the device has enrolled, and the device appears in the list.
// Open device invitations (checked against the verified log) can be revoked.
import { useEffect, useState } from 'react';
import type { OrgState } from '../../../core/log/state';
import { actionErrorCode, keyRing } from '../../../core/org/batch';
import { enrollCommand } from '../../../core/org/devices';
import {
  type CreatedInvite,
  createInvite,
  DEVICE_EXPIRY_HOURS,
  invitable,
  type InviteRow,
  openInvites,
  revokeInvite,
} from '../../../core/org/invites';
import type { Directory } from '../../../core/tasks/directory';
import { formatEventTime } from '../../../core/i18n';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useNow } from '../../countdown/useNow';
import { BUTTON, PRIMARY } from '../members/ActionDialog';
import { CopyButton, ExpirySelect } from './parts';

function useTime() {
  const { locale } = useI18n();
  const now = useNow(false);
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  return (text: string) => formatEventTime(Date.parse(text), { locale, timeZone, now });
}

function AddForm(props: {
  readonly org: string;
  readonly state: OrgState;
  readonly directory: Directory;
  readonly onCreated: (created: CreatedInvite) => void;
  readonly onCancel: () => void;
}) {
  const { org, state, directory } = props;
  const { app, origin } = useAppEnv();
  const { t, error } = useI18n();
  const held = keyRing(app.store, org);
  const choices = [...state.workgroups.keys()]
    .filter((wg) => invitable(state, held, wg))
    .sort((a, b) => Number(b === state.defaultWg) - Number(a === state.defaultWg) || Number(a > b) - Number(a < b));
  const [wgs, setWgs] = useState<string[]>(choices.includes(state.defaultWg) ? [state.defaultWg] : []);
  const [hours, setHours] = useState<number>(DEVICE_EXPIRY_HOURS);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const submit = async () => {
    setBusy(true);
    setCode(null);
    try {
      props.onCreated(
        await createInvite(app.tasks, org, {
          request: { kind: 'device', wgs, hours },
          directory,
          hubKey: app.config.hub_key,
          origin,
        }),
      );
    } catch (failure) {
      setCode(actionErrorCode(failure));
      setBusy(false);
    }
  };
  const toggle = (wg: string, on: boolean) => setWgs(on ? [...wgs, wg] : wgs.filter((item) => item !== wg));
  return (
    <form
      className="flex flex-col gap-3 rounded border border-gray-200 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      data-testid="devices.add.form"
    >
      <fieldset className="flex flex-col gap-2">
        <legend className="font-medium">{t('devices.add.workgroups')}</legend>
        {choices.length === 0 && <p data-testid="devices.add.none">{t('devices.add.none')}</p>}
        {choices.map((wg) => (
          <label key={wg} className="flex min-h-11 items-center gap-2">
            <input
              type="checkbox"
              checked={wgs.includes(wg)}
              onChange={(event) => toggle(wg, event.target.checked)}
              data-testid={`devices.add.wg.${wg}`}
            />
            <span>{directory.workgroups.get(wg) ?? t('devices.wg.unnamed')}</span>
          </label>
        ))}
      </fieldset>
      <ExpirySelect label={t('devices.expiry')} value={hours} onChange={setHours} testId="devices.add.expiry" />
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="devices.add.error" data-code={code}>
          {error(code).title}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <button type="submit" className={PRIMARY} disabled={busy || wgs.length === 0} data-testid="devices.add.submit">
          {t('devices.add.submit')}
        </button>
        <button type="button" className={BUTTON} onClick={props.onCancel} data-testid="devices.add.cancel">
          {t('devices.cancel')}
        </button>
      </div>
    </form>
  );
}

function AddResult(props: { readonly created: CreatedInvite; readonly state: OrgState; readonly onDone: () => void }) {
  const { created, state } = props;
  const { t } = useI18n();
  const { origin } = useAppEnv();
  const time = useTime();
  const command = enrollCommand(origin, created.code);
  const enrolled = (state.invites.get(created.inviteId)?.acceptedSeq ?? null) !== null;
  return (
    <section
      className="flex flex-col gap-3 rounded border border-amber-400 bg-amber-50 p-4"
      data-testid="devices.add.result"
    >
      <h2 className="font-semibold">{t('devices.add.result.title')}</h2>
      <p role="note" data-testid="devices.add.warning">
        {t('devices.add.result.warning')}
      </p>
      <p className="font-mono text-lg" data-testid="devices.add.code">
        {created.code}
      </p>
      <CopyButton
        text={created.code}
        label={t('devices.add.copy-code')}
        copiedLabel={t('devices.copied')}
        testId="devices.add.copy-code"
      />
      <p>{t('devices.add.command')}</p>
      <pre className="overflow-x-auto rounded bg-gray-900 p-3 text-sm text-white" data-testid="devices.add.command">
        {command}
      </pre>
      <CopyButton
        text={command}
        label={t('devices.add.copy-command')}
        copiedLabel={t('devices.copied')}
        testId="devices.add.copy-command"
      />
      <p data-testid="devices.add.expires">{t('devices.add.expires', { time: time(created.expires) })}</p>
      <p aria-live="polite" data-testid="devices.add.status" data-enrolled={String(enrolled)}>
        {enrolled ? t('devices.add.enrolled') : t('devices.add.waiting')}
      </p>
      <button type="button" className={PRIMARY} onClick={props.onDone} data-testid="devices.add.done">
        {t('devices.add.done')}
      </button>
    </section>
  );
}

function OpenDeviceInvites({ org, version }: { readonly org: string; readonly version: string }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const time = useTime();
  const [rows, setRows] = useState<InviteRow[] | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [revoking, setRevoking] = useState(false);
  const [reload, setReload] = useState(0);
  useEffect(() => {
    openInvites(app.tasks, org, 'device').then(
      (found) => {
        setRows(found);
        setCode(null);
      },
      (failure: unknown) => setCode(actionErrorCode(failure)),
    );
  }, [app, org, version, reload]);
  const revoke = async (id: string) => {
    setRevoking(true);
    try {
      await revokeInvite(app.tasks, org, id);
      setReload((n) => n + 1);
    } catch (failure) {
      setCode(actionErrorCode(failure));
    }
    setRevoking(false);
  };
  return (
    <section className="flex flex-col gap-2" data-testid="devices.invites">
      <h2 className="font-semibold">{t('devices.invites.title')}</h2>
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="devices.invites.error" data-code={code}>
          {error(code).title}
        </p>
      )}
      {rows !== null && rows.length === 0 && (
        <p className="text-gray-600" data-testid="devices.invites.empty">
          {t('devices.invites.empty')}
        </p>
      )}
      <ul className="flex flex-col gap-2">
        {(rows ?? []).map((row) => (
          <li key={row.invite_id} className="flex flex-wrap items-center gap-3" data-testid="devices.invites.item">
            <span>{t('devices.invites.expires', { time: time(row.expires_at) })}</span>
            <button
              type="button"
              className={BUTTON}
              disabled={revoking}
              onClick={() => void revoke(row.invite_id)}
              data-testid="devices.invites.revoke"
            >
              {t('devices.invites.revoke')}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** The admin's "Add org device" area with the open device invitations. */
export function AddDevice(props: { readonly org: string; readonly state: OrgState; readonly directory: Directory }) {
  const { t } = useI18n();
  const [mode, setMode] = useState<'closed' | 'form'>('closed');
  const [created, setCreated] = useState<CreatedInvite | null>(null);
  return (
    <div className="flex flex-col gap-3">
      {created !== null && <AddResult created={created} state={props.state} onDone={() => setCreated(null)} />}
      {created === null && mode === 'closed' && (
        <button
          type="button"
          className={`${PRIMARY} self-start`}
          onClick={() => setMode('form')}
          data-testid="devices.add.open"
        >
          {t('devices.add.open')}
        </button>
      )}
      {created === null && mode === 'form' && (
        <AddForm
          org={props.org}
          state={props.state}
          directory={props.directory}
          onCreated={(result) => {
            setCreated(result);
            setMode('closed');
          }}
          onCancel={() => setMode('closed')}
        />
      )}
      {/* the list follows the log: a new, revoked or accepted invitation changes its head */}
      <OpenDeviceInvites org={props.org} version={props.state.head} />
    </div>
  );
}
