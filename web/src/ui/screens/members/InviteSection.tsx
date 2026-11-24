// "Invite user" and the open invitations (06-web.md section 7.1; ADR-005): role (member by default), further
// workgroups with read or write (the default workgroup is always write; only workgroups whose key the admin holds),
// expiry 1 h – 30 d (72 h default). The result shows the link `<origin>/invite#<code>` and the code once, with copy
// buttons and the warning to pass them on confidentially; "Done" forgets them. Open invitations (checked against the
// verified log) can be revoked.
import { useEffect, useId, useState } from 'react';
import { type Access, type Role } from '../../../core/org/members';
import {
  type CreatedInvite,
  createInvite,
  EXPIRY_HOURS,
  invitable,
  type InviteRow,
  openInvites,
  revokeInvite,
  USER_EXPIRY_HOURS,
} from '../../../core/org/invites';
import { actionErrorCode, keyRing } from '../../../core/org/batch';
import type { OrgState } from '../../../core/log/state';
import type { Directory } from '../../../core/tasks/directory';
import { formatEventTime } from '../../../core/i18n';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { useNow } from '../../countdown/useNow';
import { BUTTON, PRIMARY } from './ActionDialog';

const clipboardWrite = (text: string) => navigator.clipboard.writeText(text);

/** `72 h` / `3 d` style label key and count of an expiry in hours. */
export function expiryLabel(hours: number): { key: string; count: number } {
  return hours % 24 === 0
    ? { key: 'members.invite.expiry.days', count: hours / 24 }
    : { key: 'members.invite.expiry.hours', count: hours };
}

interface InviteFormProps {
  readonly org: string;
  readonly state: OrgState;
  readonly directory: Directory;
  readonly onCreated: (created: CreatedInvite) => void;
  readonly onCancel: () => void;
}

function InviteForm({ org, state, directory, onCreated, onCancel }: InviteFormProps) {
  const { app, origin } = useAppEnv();
  const { t, error } = useI18n();
  const ids = { role: useId(), expiry: useId() };
  const [role, setRole] = useState<Role>('member');
  const [hours, setHours] = useState<number>(USER_EXPIRY_HOURS);
  const [wgs, setWgs] = useState<Record<string, Access>>({});
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const held = keyRing(app.store, org);
  const choices = [...state.workgroups.keys()]
    .filter((wg) => wg !== state.defaultWg && invitable(state, held, wg))
    .sort();
  const submit = async () => {
    setBusy(true);
    setCode(null);
    try {
      const created = await createInvite(app.tasks, org, {
        request: { kind: 'user', role, wgs, hours },
        directory,
        hubKey: app.config.hub_key,
        origin,
      });
      onCreated(created);
    } catch (failure) {
      setCode(actionErrorCode(failure));
    }
    setBusy(false);
  };
  const choose = (wg: string, value: string) => {
    const next = { ...wgs };
    if (value === '') delete next[wg];
    else next[wg] = value as Access;
    setWgs(next);
  };
  return (
    <form
      className="flex flex-col gap-3 rounded border border-gray-200 p-4"
      onSubmit={(event) => {
        event.preventDefault();
        void submit();
      }}
      data-testid="members.invite.form"
    >
      <label htmlFor={ids.role} className="font-medium">
        {t('members.invite.role')}
      </label>
      <select
        id={ids.role}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={role}
        onChange={(event) => setRole(event.target.value as Role)}
        data-testid="members.invite.role"
      >
        <option value="member">{t('members.role.member')}</option>
        <option value="admin">{t('members.role.admin')}</option>
      </select>
      <fieldset className="flex flex-col gap-2">
        <legend className="font-medium">{t('members.invite.workgroups')}</legend>
        <p className="text-sm text-gray-700" data-testid="members.invite.default">
          {t('members.invite.default', { name: directory.workgroups.get(state.defaultWg) ?? t('members.wg.unnamed') })}
        </p>
        {choices.map((wg) => (
          <label key={wg} className="flex items-center justify-between gap-2">
            <span>{directory.workgroups.get(wg) ?? t('members.wg.unnamed')}</span>
            <select
              className="min-h-11 rounded border border-gray-300 px-2"
              value={wgs[wg] ?? ''}
              onChange={(event) => choose(wg, event.target.value)}
              data-testid={`members.invite.wg.${wg}`}
            >
              <option value="">{t('members.access.none')}</option>
              <option value="read">{t('members.access.read')}</option>
              <option value="write">{t('members.access.write')}</option>
            </select>
          </label>
        ))}
      </fieldset>
      <label htmlFor={ids.expiry} className="font-medium">
        {t('members.invite.expiry')}
      </label>
      <select
        id={ids.expiry}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={hours}
        onChange={(event) => setHours(Number(event.target.value))}
        data-testid="members.invite.expiry"
      >
        {EXPIRY_HOURS.map((value) => {
          const label = expiryLabel(value);
          return (
            <option key={value} value={value}>
              {t(label.key, { count: label.count })}
            </option>
          );
        })}
      </select>
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="members.invite.error" data-code={code}>
          {error(code).title}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <button type="submit" className={PRIMARY} disabled={busy} data-testid="members.invite.submit">
          {t('members.invite.submit')}
        </button>
        <button type="button" className={BUTTON} onClick={onCancel} data-testid="members.invite.cancel">
          {t('members.cancel')}
        </button>
      </div>
    </form>
  );
}

function InviteResult({ created, onDone }: { readonly created: CreatedInvite; readonly onDone: () => void }) {
  const { t } = useI18n();
  const [copied, setCopied] = useState<string | null>(null);
  const copy = (what: string, text: string) => {
    clipboardWrite(text).then(
      () => setCopied(what),
      () => setCopied(null),
    );
  };
  return (
    <section
      className="flex flex-col gap-3 rounded border border-amber-400 bg-amber-50 p-4"
      data-testid="members.invite.result"
    >
      <h2 className="font-semibold">{t('members.invite.result.title')}</h2>
      <p role="note" data-testid="members.invite.warning">
        {t('members.invite.result.warning')}
      </p>
      <p className="break-all font-mono text-sm" data-testid="members.invite.link">
        {created.link}
      </p>
      <button
        type="button"
        className={BUTTON}
        onClick={() => copy('link', created.link)}
        data-testid="members.invite.copy-link"
      >
        {copied === 'link' ? t('members.invite.copied') : t('members.invite.copy-link')}
      </button>
      <p className="font-mono text-lg" data-testid="members.invite.code">
        {created.code}
      </p>
      <button
        type="button"
        className={BUTTON}
        onClick={() => copy('code', created.code)}
        data-testid="members.invite.copy-code"
      >
        {copied === 'code' ? t('members.invite.copied') : t('members.invite.copy-code')}
      </button>
      <button type="button" className={PRIMARY} onClick={onDone} data-testid="members.invite.done">
        {t('members.invite.done')}
      </button>
    </section>
  );
}

function OpenInvites({ org, version }: { readonly org: string; readonly version: number }) {
  const { app } = useAppEnv();
  const { t, error, locale } = useI18n();
  const [rows, setRows] = useState<InviteRow[] | null>(null);
  const [code, setCode] = useState<string | null>(null);
  const [revoking, setRevoking] = useState<string | null>(null);
  const [reload, setReload] = useState(0);
  const now = useNow(false);
  useEffect(() => {
    // a late answer after unmounting sets nothing (React ignores it); the list is reloaded on every change anyway
    openInvites(app.tasks, org, 'user').then(
      (found) => {
        setRows(found);
        setCode(null);
      },
      (failure: unknown) => setCode(actionErrorCode(failure)),
    );
  }, [app, org, version, reload]);
  const revoke = async (id: string) => {
    setRevoking(id);
    try {
      await revokeInvite(app.tasks, org, id);
      setReload((n) => n + 1);
    } catch (failure) {
      setCode(actionErrorCode(failure));
    }
    setRevoking(null);
  };
  const timeZone = Intl.DateTimeFormat().resolvedOptions().timeZone;
  const time = (text: string) => formatEventTime(Date.parse(text), { locale, timeZone, now });
  return (
    <section className="flex flex-col gap-2" data-testid="members.invites">
      <h2 className="font-semibold">{t('members.invites.title')}</h2>
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="members.invites.error" data-code={code}>
          {error(code).title}
        </p>
      )}
      {rows !== null && rows.length === 0 && (
        <p className="text-gray-600" data-testid="members.invites.empty">
          {t('members.invites.empty')}
        </p>
      )}
      <ul className="flex flex-col gap-2">
        {(rows ?? []).map((row) => (
          <li key={row.invite_id} className="flex flex-wrap items-center gap-3" data-testid="members.invites.item">
            <span>{t('members.invites.expires', { time: time(row.expires_at) })}</span>
            <button
              type="button"
              className={BUTTON}
              disabled={revoking !== null}
              onClick={() => void revoke(row.invite_id)}
              data-testid="members.invites.revoke"
            >
              {t('members.invites.revoke')}
            </button>
          </li>
        ))}
      </ul>
    </section>
  );
}

/** The admin's invitation area. */
export function InviteSection({
  org,
  state,
  directory,
}: {
  readonly org: string;
  readonly state: OrgState;
  readonly directory: Directory;
}) {
  const { t } = useI18n();
  const [mode, setMode] = useState<'closed' | 'form'>('closed');
  const [created, setCreated] = useState<CreatedInvite | null>(null);
  const [version, setVersion] = useState(0);
  return (
    <div className="flex flex-col gap-3">
      {created !== null && (
        <InviteResult
          created={created}
          onDone={() => {
            setCreated(null);
          }}
        />
      )}
      {created === null && mode === 'closed' && (
        <button
          type="button"
          className={`${PRIMARY} self-start`}
          onClick={() => setMode('form')}
          data-testid="members.invite.open"
        >
          {t('members.invite.open')}
        </button>
      )}
      {created === null && mode === 'form' && (
        <InviteForm
          org={org}
          state={state}
          directory={directory}
          onCreated={(result) => {
            setCreated(result);
            setMode('closed');
            setVersion((v) => v + 1);
          }}
          onCancel={() => setMode('closed')}
        />
      )}
      <OpenInvites org={org} version={version} />
    </div>
  );
}
