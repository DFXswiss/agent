// Branch of the invitation screen for org invitations (06-web.md sections 3.1 and 3.1.1, 04-crypto.md sections 5.1,
// 5.2 and 5.2a), registered at WEB-03's `org` step: `org_verify` (package opened with the code, lookup log verified)
// → `intro` (org and workgroup names from the package, role and expiry from the verified `invite` entry; display
// name) → either "Create passkey" (gesture 1, gesture 2 – the registration of WEB-03; a passkey without PRF output
// ends in `prf_required` with the registration withdrawn, KOR-22) or "I already
// have an account" (`existing_account`: sign-in with the passkey, else this browser's device key, else the
// `prf_missing` guidance of WEB-14) → `accept`
// (`accept` with `sig` and `inv_sig`, self-wraps of the current package keys, `member.profile`) → the remaining
// self-wraps queued for the job, the org installed and remembered as the last org → `/o/:org`. A signed-in user goes
// to `existing_account` directly. Cancelled gestures return to the step with "try again"; refusals of the package or
// the log block (nothing is sent); `invite_registered` and invalid invitations end in the invalid state.
import { type FormEvent, useEffect, useState } from 'react';
import { Navigate } from 'react-router-dom';
import { CeremonyCancelled } from '../../../../core/account/ceremony';
import { displayName } from '../../../../core/account/genesis';
import {
  completeInvitation,
  type Joiner,
  joinOrg,
  loadPlatform,
  openInvitation,
  type OrgInvitation,
} from '../../../../core/account/join';
import { type SignIn, signIn } from '../../../../core/account/login';
import { type Lookup, register } from '../../../../core/account/register';
import { meOrgs } from '../../../../core/account/session';
import type { Identity } from '../../../../core/crypto/keys';
import { parseTime } from '../../../../core/encoding/time';
import { formatEventTime } from '../../../../core/i18n';
import { queueSelfWraps } from '../../../../core/jobs/self-wraps';
import { verifiedPlatform } from '../../../../core/org/batch';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { errorCode } from '../../../shell/errors';
import { unlockSignIn, uploadMissingSlot, withdrawRegistration } from '../../../prf-missing/device';
import { PrfRequired } from '../../../prf-missing/PrfRequired';
import { PrfMissing } from '../../../prf-missing/PrfMissing';
import { afterVerify, failedStep, type OrgStep, verifyFailed } from './machine';

export interface OrgInviteProps {
  readonly code: string;
  readonly lookup: Lookup;
}

const PRIMARY = 'min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60';
const SECONDARY = 'min-h-11 rounded border border-gray-900 px-4 disabled:opacity-60';
const INPUT = 'min-h-11 rounded border border-gray-400 px-3';

type Step<S extends OrgStep['step']> = Extract<OrgStep, { step: S }>;

export function OrgInvite({ code, lookup }: OrgInviteProps) {
  const { app, credentials, origin } = useAppEnv();
  const { t, error, locale } = useI18n();
  const [step, setStep] = useState<OrgStep>({ step: 'org_verify' });
  const [name, setName] = useState('');
  const [nameInvalid, setNameInvalid] = useState(false);

  useEffect(() => {
    let live = true;
    const verify = async () => {
      let next: OrgStep;
      try {
        const invitation = await openInvitation(code, lookup, verifiedPlatform(app.store));
        next = afterVerify(invitation, app.store.getState().session.phase);
      } catch (caught) {
        next = verifyFailed(caught);
      }
      if (live) setStep(next);
    };
    void verify();
    return () => {
      live = false;
    };
  }, [app, code, lookup]);

  /** The display name, or null (with the inline error) if it is not 1–80 characters. */
  const checkedName = (): string | null => {
    const checked = displayName(name);
    setNameInvalid(checked === null);
    return checked;
  };

  const accept = async (invitation: OrgInvitation, joiner: Joiner) => {
    setStep({ step: 'accept', invitation, failed: null });
    const request = (method: string, target: string, options?: { body: unknown }) =>
      app.client.request(method, target, options);
    try {
      const platform = verifiedPlatform(app.store) ?? (await loadPlatform(request, app.config.platform_root_key));
      const complete = await completeInvitation(invitation, code, platform);
      const isMember = async (org: string) => meOrgs(await app.loadMe()).some((item) => item.org_id === org);
      const joined = await joinOrg(
        { anon: app.anon, request, isMember },
        { invitation: complete, joiner, displayName: name, platform, code },
      );
      await app.installOrg(joined.prepared);
      // the display name waits for the current org key if the batch could not carry `member.profile`
      const profile = joined.prepared.profile ? null : displayName(name);
      queueSelfWraps(app.store, joined.prepared.org, joiner.userId, joined.invitation.keys, profile);
      void app.tasks.refreshLog(joined.prepared.org);
      setStep({ step: 'done', org: joined.prepared.org });
    } catch (caught) {
      setStep(failedStep(caught) ?? { step: 'accept', invitation, failed: errorCode(caught) });
    }
  };

  // --- new user: the registration of WEB-03 (gesture 1, gesture 2; without PRF `prf_required`) ---

  const createPasskey = async (event: FormEvent, current: Step<'intro'>) => {
    event.preventDefault();
    if (checkedName() === null) return;
    setStep({ step: 'create_passkey', invitation: current.invitation });
    try {
      const registration = await register(app.anon, credentials, code);
      setStep({ step: 'unlock_passkey', invitation: current.invitation, registration, busy: false, notice: null });
    } catch (caught) {
      const notice = caught instanceof CeremonyCancelled ? t('invite.cancelled') : error(errorCode(caught)).title;
      setStep(failedStep(caught) ?? { ...current, notice });
    }
  };

  const unlockNew = async (current: Step<'unlock_passkey'>) => {
    setStep({ ...current, busy: true, notice: null });
    try {
      const result = await signIn({ anon: app.anon, credentials, origin });
      if (result.userId !== current.registration.userId) {
        setStep({ ...current, busy: false, notice: t('invite.unlock.other-account') });
        return;
      }
      await app.begin(result);
      await app.unlock(current.registration.identity);
      const slot = await app.uploadPasskeySlot(result, current.registration.identity);
      if (!slot) {
        setStep({ step: 'prf_required', invitation: current.invitation, released: await withdrawRegistration(app) });
        return;
      }
      app.notePrf(result, slot);
      const { userId, identity } = current.registration;
      void accept(current.invitation, { userId, identity });
    } catch (caught) {
      const notice =
        caught instanceof CeremonyCancelled ? t('invite.unlock.cancelled') : error(errorCode(caught)).title;
      setStep({ ...current, busy: false, notice });
    }
  };

  // --- existing account (04-crypto.md section 5.2a) ---

  const toExisting = (invitation: OrgInvitation) =>
    setStep({ step: 'existing_account', invitation, mode: 'sign_in', signIn: null, busy: false, notice: null });

  const ready = (current: Step<'existing_account'>) =>
    setStep({ ...current, mode: 'ready', signIn: null, busy: false, notice: null });

  const unlocked = async (current: Step<'existing_account'>, result: SignIn, identity: Identity) => {
    await app.unlock(identity);
    // unlocked by the device key: a PRF-capable passkey without slot gets its slot now
    await uploadMissingSlot(app, result, identity);
    ready(current);
  };

  const signInExisting = async (current: Step<'existing_account'>) => {
    setStep({ ...current, busy: true, notice: null });
    let result: SignIn;
    try {
      result = await signIn({ anon: app.anon, credentials, origin });
      await app.begin(result);
    } catch (caught) {
      const notice =
        caught instanceof CeremonyCancelled ? t('invite-org.existing.cancelled') : error(errorCode(caught)).title;
      setStep({ ...current, busy: false, notice });
      return;
    }
    const recovery = { ...current, mode: 'recovery', signIn: result, busy: false, notice: null } as const;
    const unlocking = await unlockSignIn(app, result);
    if (unlocking === null) {
      setStep(recovery);
      return;
    }
    try {
      await unlocked(current, result, unlocking);
    } catch (caught) {
      const failed = errorCode(caught);
      setStep(failed === 'decrypt_failed' ? recovery : { ...current, busy: false, notice: error(failed).title });
    }
  };

  const joinExisting = (event: FormEvent, current: Step<'existing_account'>) => {
    event.preventDefault();
    if (checkedName() === null) return;
    const { userId, identity } = app.store.getState().me;
    // already a member (the verified log says so): nothing to accept, the org opens
    if (current.invitation.log?.state.member(userId!)) setStep({ step: 'done', org: current.invitation.org });
    else void accept(current.invitation, { userId: userId!, identity: identity! });
  };

  const retry = (current: Step<'accept'>) => {
    const { userId, identity } = app.store.getState().me;
    void accept(current.invitation, { userId: userId!, identity: identity! });
  };

  if (step.step === 'done') return <Navigate to={`/o/${step.org}`} replace />;

  const orgName = (invitation: OrgInvitation) => invitation.orgName || t('invite-org.org-unnamed');

  const nameField = (disabled: boolean) => (
    <>
      <label htmlFor="invite-display-name">{t('invite.name.label')}</label>
      <input
        id="invite-display-name"
        className={INPUT}
        autoComplete="name"
        maxLength={200}
        value={name}
        aria-invalid={nameInvalid}
        aria-describedby="invite-display-name-hint"
        onChange={(event) => {
          setName(event.target.value);
          setNameInvalid(false);
        }}
        disabled={disabled}
        data-testid="invite.display-name"
      />
      <p id="invite-display-name-hint" className="text-sm text-gray-700">
        {t('invite.name.hint')}
      </p>
      {nameInvalid && (
        <p role="alert" className="text-red-800" data-testid="invite.display-name.invalid">
          {t('invite.name.invalid')}
        </p>
      )}
    </>
  );

  const notice = (text: string | null) =>
    text !== null && (
      <p role="alert" data-testid="invite.notice">
        {text}
      </p>
    );

  const summary = (invitation: OrgInvitation) => (
    <div className="flex flex-col gap-2" data-testid="invite.org-summary">
      <p data-testid="invite.role">{t('invite-org.intro.role', { role: t(`invite-org.role.${invitation.role}`) })}</p>
      <p>{t('invite-org.intro.workgroups')}</p>
      <ul className="list-disc pl-6">
        {invitation.workgroups.map((wg) => (
          <li key={wg.id} data-testid="invite.workgroup" data-access={wg.access}>
            {wg.name || t('invite-org.workgroup.unnamed')}
            {' – '}
            {t(`invite-org.access.${wg.access}`)}
          </li>
        ))}
      </ul>
      <p className="text-sm text-gray-700" data-testid="invite.expires">
        {t('invite.intro.expires', {
          time: formatEventTime(parseTime(invitation.expires), {
            locale,
            timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
            // an expiry always shows its date
            now: 0,
          }),
        })}
      </p>
    </div>
  );

  return (
    <section className="flex flex-col gap-4" data-testid="invite.org" data-step={step.step}>
      {step.step === 'org_verify' && <p data-testid="invite.verifying">{t('invite-org.verifying')}</p>}
      {step.step === 'blocked' && (
        <section role="alert" className="flex flex-col gap-2" data-testid="invite.blocked" data-code={step.code}>
          <h2 className="text-xl font-semibold">{t('invite-org.blocked.title')}</h2>
          <p>{t('invite-org.blocked.body')}</p>
          <p className="text-sm text-gray-700">{error(step.code).title}</p>
        </section>
      )}
      {step.step === 'invalid' && (
        <section role="alert" data-testid="invite.invalid" data-code={step.code}>
          <h2 className="text-xl font-semibold">{error(step.code).title}</h2>
          <p>{error(step.code).body}</p>
          <p>{error(step.code).action}</p>
        </section>
      )}
      {(step.step === 'intro' || step.step === 'create_passkey') && (
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => step.step === 'intro' && void createPasskey(event, step)}
          data-testid="invite.org-intro"
        >
          <h2 className="text-xl font-semibold" data-testid="invite.org-name">
            {t('invite-org.intro.title', { org: orgName(step.invitation) })}
          </h2>
          {summary(step.invitation)}
          {nameField(step.step === 'create_passkey')}
          <p>{t('invite.create.explain')}</p>
          <button
            type="submit"
            className={PRIMARY}
            disabled={step.step === 'create_passkey'}
            data-testid="invite.create-passkey"
          >
            {step.step === 'create_passkey' ? t('invite.create.working') : t('invite.create-passkey')}
          </button>
          <button
            type="button"
            className={SECONDARY}
            disabled={step.step === 'create_passkey'}
            onClick={() => toExisting(step.invitation)}
            data-testid="invite.existing-account"
          >
            {t('invite-org.existing')}
          </button>
          {step.step === 'intro' && notice(step.notice)}
        </form>
      )}
      {step.step === 'unlock_passkey' && (
        <section className="flex flex-col gap-3" data-testid="invite.unlock-step">
          <h2 className="text-xl font-semibold">{t('invite.unlock.title')}</h2>
          <p>{t('invite.unlock.explain')}</p>
          <button
            type="button"
            className={PRIMARY}
            onClick={() => void unlockNew(step)}
            disabled={step.busy}
            data-testid="invite.unlock"
          >
            {step.busy ? t('invite.unlock.working') : t('invite.unlock')}
          </button>
          {notice(step.notice)}
        </section>
      )}
      {step.step === 'prf_required' && (
        <PrfRequired
          context="register"
          released={step.released}
          onRetry={() => setStep({ step: 'intro', invitation: step.invitation, notice: null })}
        />
      )}
      {step.step === 'existing_account' && (
        <section className="flex flex-col gap-3" data-testid="invite.existing" data-mode={step.mode}>
          <h2 className="text-xl font-semibold" data-testid="invite.org-name">
            {t('invite-org.existing.title', { org: orgName(step.invitation) })}
          </h2>
          {summary(step.invitation)}
          {step.mode === 'sign_in' && (
            <>
              <p>{t('invite-org.existing.explain')}</p>
              <button
                type="button"
                className={PRIMARY}
                onClick={() => void signInExisting(step)}
                disabled={step.busy}
                data-testid="invite.sign-in"
              >
                {step.busy ? t('invite.unlock.working') : t('invite.unlock')}
              </button>
            </>
          )}
          {step.mode === 'recovery' && (
            <PrfMissing
              signIn={step.signIn!}
              unlock={(identity) => app.unlock(identity)}
              onUnlocked={() => ready(step)}
            />
          )}
          {step.mode === 'ready' && (
            <form className="flex flex-col gap-3" onSubmit={(event) => joinExisting(event, step)}>
              {nameField(false)}
              <button type="submit" className={PRIMARY} data-testid="invite.join">
                {t('invite-org.join', { org: orgName(step.invitation) })}
              </button>
            </form>
          )}
          {step.mode !== 'ready' && (
            <button
              type="button"
              className="min-h-11 underline"
              onClick={() => setStep({ step: 'intro', invitation: step.invitation, notice: null })}
              disabled={step.busy}
              data-testid="invite.back"
            >
              {t('invite-org.back')}
            </button>
          )}
          {notice(step.notice)}
        </section>
      )}
      {step.step === 'accept' &&
        (step.failed === null ? (
          <p data-testid="invite.accepting">{t('invite-org.accept.working', { org: orgName(step.invitation) })}</p>
        ) : (
          <section
            role="alert"
            className="flex flex-col gap-3"
            data-testid="invite.accept-failed"
            data-code={step.failed}
          >
            <p>{error(step.failed).title}</p>
            <button type="button" className={PRIMARY} onClick={() => retry(step)} data-testid="invite.accept-retry">
              {t('invite.accept.retry')}
            </button>
          </section>
        ))}
    </section>
  );
}
