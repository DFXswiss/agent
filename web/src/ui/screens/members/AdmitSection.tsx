// "Add an existing account" (06-web.md section 7.1, ADR-060): the admin types the account code the person sees in its
// settings, or picks one of the accounts registered through the admin's own platform invitations; chooses the role and
// the workgroups (the default workgroup is always write), enters the person's name and admits with one button. The
// name is the provisional name every member sees until the person's own one is written (ADR-067, `member.name`); an
// open invitation the same account registered with is closed right after. The person does nothing; it finds the
// organization the next time it opens the app. The identity shown before the button is the proven one (checked
// against the platform log in `core/org/admit.ts`), never the hub's word.
import { useEffect, useId, useState } from 'react';
import { formatAccountCode } from '../../../core/crypto/identity';
import { admitAccount, findAccount, type Invitee, loadInvitees, type ProvenIdentity } from '../../../core/org/admit';
import { actionErrorCode, refuse, verifiedPlatform } from '../../../core/org/batch';
import { type Access, type Role } from '../../../core/org/members';
import { closeSettledInvites, giveName } from '../../../core/org/names';
import { displayName } from '../../../core/account/genesis';
import type { OrgState } from '../../../core/log/state';
import type { Directory } from '../../../core/tasks/directory';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../../shell/context';
import { BUTTON, PRIMARY } from './ActionDialog';

/** An admitted account with its name and whether the name could be saved (else it can be entered at the entry). */
interface Admitted {
  readonly identity: ProvenIdentity;
  readonly name: string;
  readonly named: boolean;
}

interface AdmitFormProps {
  readonly org: string;
  readonly state: OrgState;
  readonly directory: Directory;
  readonly onDone: (admitted: Admitted) => void;
  readonly onCancel: () => void;
}

function AdmitForm({ org, state, directory, onDone, onCancel }: AdmitFormProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const ids = { code: useId(), role: useId(), name: useId() };
  const [text, setText] = useState('');
  const [name, setName] = useState('');
  const [found, setFound] = useState<ProvenIdentity | null>(null);
  const [invitees, setInvitees] = useState<Invitee[]>([]);
  const [role, setRole] = useState<Role>('member');
  const [wgs, setWgs] = useState<Record<string, Access>>({});
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  useEffect(() => {
    // the list is a convenience: a failure leaves it empty, the code field still works
    loadInvitees(app.tasks).then(
      (rows) => setInvitees(rows.filter((row) => row.identity !== null && state.member(row.userId) === null)),
      () => setInvitees([]),
    );
  }, [app, state]);
  const choices = [...state.workgroups.keys()].filter((wg) => wg !== state.defaultWg && state.writable(wg)).sort();
  const look = async () => {
    setBusy(true);
    setCode(null);
    setFound(null);
    try {
      const platform = verifiedPlatform(app.store) ?? refuse('identity_unproven');
      const identity = await findAccount(app.tasks.client, platform, { code: text });
      if (state.member(identity.userId) !== null) refuse('already_member');
      setFound(identity);
    } catch (failure) {
      setCode(actionErrorCode(failure));
    }
    setBusy(false);
  };
  const admit = async (subject: ProvenIdentity) => {
    setBusy(true);
    setCode(null);
    try {
      await admitAccount(app.tasks, org, subject, role, wgs);
      // the admission stands; the name and the settled invitation follow (a failure leaves "Enter name" at the entry)
      const named = await giveName(app.tasks, org, subject.userId, name).then(
        () => true,
        () => false,
      );
      await closeSettledInvites(app.tasks, org, subject.userId).catch(() => []);
      onDone({ identity: subject, name: displayName(name)!, named });
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
    <section className="flex flex-col gap-3 rounded border border-gray-200 p-4" data-testid="members.admit.form">
      <h2 className="font-semibold">{t('members.admit.title')}</h2>
      <p className="text-sm text-gray-700">{t('members.admit.hint')}</p>
      <form
        className="flex flex-wrap items-end gap-2"
        onSubmit={(event) => {
          event.preventDefault();
          void look();
        }}
      >
        <label htmlFor={ids.code} className="flex flex-col gap-1 font-medium">
          {t('members.admit.code')}
          <input
            id={ids.code}
            className="min-h-11 rounded border border-gray-300 px-2 font-mono uppercase"
            value={text}
            autoComplete="off"
            spellCheck={false}
            placeholder={t('members.admit.placeholder')}
            onChange={(event) => setText(event.target.value)}
            data-testid="members.admit.code"
          />
        </label>
        <button type="submit" className={BUTTON} disabled={busy || text.trim() === ''} data-testid="members.admit.find">
          {t('members.admit.find')}
        </button>
      </form>
      {invitees.length > 0 && (
        <fieldset className="flex flex-col gap-1" data-testid="members.admit.invitees">
          <legend className="font-medium">{t('members.admit.invitees')}</legend>
          {invitees.map((row) => (
            <button
              key={row.userId}
              type="button"
              className={`${BUTTON} self-start font-mono`}
              onClick={() => setFound(row.identity)}
              data-testid="members.admit.invitee"
            >
              {formatAccountCode(row.identity!.accountCode)}
            </button>
          ))}
        </fieldset>
      )}
      {found !== null && (
        <p className="font-mono" data-testid="members.admit.found">
          {t('members.admit.found', { code: formatAccountCode(found.accountCode) })}
        </p>
      )}
      <label htmlFor={ids.name} className="flex flex-col gap-1 font-medium">
        {t('names.give.label')}
        <input
          id={ids.name}
          className="min-h-11 rounded border border-gray-300 px-2"
          value={name}
          maxLength={200}
          autoComplete="off"
          onChange={(event) => setName(event.target.value)}
          data-testid="members.admit.name"
        />
      </label>
      <p className="text-sm text-gray-700">{t('members.admit.name-hint')}</p>
      <label htmlFor={ids.role} className="font-medium">
        {t('members.invite.role')}
      </label>
      <select
        id={ids.role}
        className="min-h-11 rounded border border-gray-300 px-2"
        value={role}
        onChange={(event) => setRole(event.target.value as Role)}
        data-testid="members.admit.role"
      >
        <option value="member">{t('members.role.member')}</option>
        <option value="admin">{t('members.role.admin')}</option>
      </select>
      <fieldset className="flex flex-col gap-2">
        <legend className="font-medium">{t('members.invite.workgroups')}</legend>
        <p className="text-sm text-gray-700">
          {t('members.invite.default', { name: directory.workgroups.get(state.defaultWg) ?? t('members.wg.unnamed') })}
        </p>
        {choices.map((wg) => (
          <label key={wg} className="flex items-center justify-between gap-2">
            <span>{directory.workgroups.get(wg) ?? t('members.wg.unnamed')}</span>
            <select
              className="min-h-11 rounded border border-gray-300 px-2"
              value={wgs[wg] ?? ''}
              onChange={(event) => choose(wg, event.target.value)}
              data-testid={`members.admit.wg.${wg}`}
            >
              <option value="">{t('members.access.none')}</option>
              <option value="read">{t('members.access.read')}</option>
              <option value="write">{t('members.access.write')}</option>
            </select>
          </label>
        ))}
      </fieldset>
      {code !== null && (
        <p role="alert" className="text-red-800" data-testid="members.admit.error" data-code={code}>
          {code === 'already_member' ? t('members.admit.already') : error(code).title}
        </p>
      )}
      <div className="flex flex-wrap gap-3">
        <button
          type="button"
          className={PRIMARY}
          disabled={busy || found === null || displayName(name) === null}
          onClick={() => found !== null && void admit(found)}
          data-testid="members.admit.submit"
        >
          {t('members.admit.submit')}
        </button>
        <button type="button" className={BUTTON} onClick={onCancel} data-testid="members.admit.cancel">
          {t('members.cancel')}
        </button>
      </div>
    </section>
  );
}

/** The admin's area to admit existing accounts. */
export function AdmitSection({
  org,
  state,
  directory,
}: {
  readonly org: string;
  readonly state: OrgState;
  readonly directory: Directory;
}) {
  const { t } = useI18n();
  const [open, setOpen] = useState(false);
  const [admitted, setAdmitted] = useState<Admitted | null>(null);
  return (
    <div className="flex flex-col gap-3">
      {admitted !== null && (
        <p role="status" className="rounded bg-green-50 p-3 text-green-900" data-testid="members.admit.done">
          {t('members.admit.done', { name: admitted.name, code: formatAccountCode(admitted.identity.accountCode) })}
          {!admitted.named && ` ${t('members.admit.unnamed')}`}
        </p>
      )}
      {!open && (
        <button
          type="button"
          className={`${PRIMARY} self-start`}
          onClick={() => {
            setAdmitted(null);
            setOpen(true);
          }}
          data-testid="members.admit.open"
        >
          {t('members.admit.open')}
        </button>
      )}
      {open && (
        <AdmitForm
          org={org}
          state={state}
          directory={directory}
          onDone={(subject) => {
            setAdmitted(subject);
            setOpen(false);
          }}
          onCancel={() => setOpen(false)}
        />
      )}
    </div>
  );
}
