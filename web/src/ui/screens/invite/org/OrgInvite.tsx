// Branch of the invitation screen for org invitations (06-web.md sections 3.1 and 3.1.1, 04-crypto.md sections 5.1,
// 5.2 and 5.2a), registered at WEB-03's `org` step. `org_verify` (package opened with the code, lookup log verified;
// it waits until the stored session was restored) → with a stored session `existing_account`, else `intro`.
// - `existing_account` (KOR-36): one main button joins with the account of this browser – "Join <org> as <name>" when
//   the keys are unlocked (the name read from the account's own `member.profile`), else "Join <org> with my account",
//   which signs in with the passkey, unlocks and joins in one go (a passkey that cannot unlock: this browser's device
//   key, else the recovery code of WEB-14's `prf_missing` guidance, and the join follows). The name in the new org is
//   the account's name; changing it is optional ("Use another name"). Side paths: "Use another account" (sign in with
//   any passkey) and "Create a new account".
// - `intro` (nobody signed in): "I already have an account – sign in and join" first (the same one-go join), then
//   "New here – create an account with a passkey" with the display name (gesture 1, gesture 2 – the registration of
//   WEB-03; a passkey without PRF output ends in `prf_required` with the registration withdrawn, KOR-22), and a sentence
//   against a second account by mistake.
// → `accept` (`accept` with `sig` and `inv_sig`, self-wraps of the current package keys, `member.profile`) → the
// remaining self-wraps queued for the job, the org installed and remembered as the last org → `/o/:org` with "You are
// now a member of <org>" (`joined` slice). Until then every step says that the user has not joined yet. A cancelled
// gesture or a failure (hub unavailable, new release) keeps the step with the message and "Try again" – the code stays
// in this screen's memory, the link is not needed again; an accept whose answer was lost is recognized by the
// membership on the retry. Refusals of the package or the log block (nothing is sent); `invite_registered` and invalid
// invitations end in the invalid state.
// With the pairing of the user's AI agent (11-agent-access.md section 5.2) the page names the agent, the proposed
// display name replaces the name field, both sign-ins are pairing-tagged, and `/connect` finishes the pairing.
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
import { ownDisplayName } from '../../../../core/account/own-name';
import { type Lookup, register } from '../../../../core/account/register';
import { meOrgs } from '../../../../core/account/session';
import type { PairingLink } from '../../../../core/agents/link';
import type { Identity } from '../../../../core/crypto/keys';
import { parseTime } from '../../../../core/encoding/time';
import { formatEventTime } from '../../../../core/i18n';
import { queueSelfWraps } from '../../../../core/jobs/self-wraps';
import { verifiedPlatform } from '../../../../core/org/batch';
import { noteJoined } from '../../../../core/store/slices/joined';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { errorCode } from '../../../shell/errors';
import { unlockSignIn, uploadMissingSlot, withdrawRegistration } from '../../../prf-missing/device';
import { PrfRequired } from '../../../prf-missing/PrfRequired';
import { PrfMissing } from '../../../prf-missing/PrfMissing';
import { useAgentCode } from '../../connect/AgentCode';
import { AgentNotice, ProposedName } from '../AgentNotice';
import { afterVerify, existingStep, failedStep, type OrgStep, settledPhase, verifyFailed } from './machine';

export interface OrgInviteProps {
  readonly code: string;
  readonly lookup: Lookup;
  /** The pairing of the agent's invitation, or null. */
  readonly pairing?: PairingLink | null;
  /** The agent code of a remote pairing the user must type before a passkey gesture (ADR-067), or null. */
  readonly agentCode?: string | null;
  /** The display name the agent proposed, or null. */
  readonly proposedName?: string | null;
}

const PRIMARY = 'min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60';
const INPUT = 'min-h-11 rounded border border-gray-400 px-3';
const LINK = 'min-h-11 self-start underline disabled:opacity-60';

type Step<S extends OrgStep['step']> = Extract<OrgStep, { step: S }>;

export function OrgInvite({
  code,
  lookup,
  pairing: linked = null,
  agentCode: expectedCode = null,
  proposedName = null,
}: OrgInviteProps) {
  const { app, credentials, origin } = useAppEnv();
  const { t, error, locale } = useI18n();
  const [step, setStep] = useState<OrgStep>({ step: 'org_verify' });
  const [name, setName] = useState(proposedName ?? '');
  /** The agent's pairing; dropped when it expired before a sign-in (the invitation is then accepted without it). */
  const [pairing, setPairing] = useState(linked);
  const tag = pairing === null ? {} : { pairingId: pairing.pairingId };
  const agentCode = useAgentCode(expectedCode);
  /** A remote pairing's code must match before any passkey gesture (ADR-067 section 6). */
  const codeOk = pairing === null || agentCode.matches;
  /** The notice of a failed sign-in; an expired pairing is dropped so that the next sign-in works without it. */
  const signInNotice = (caught: unknown, cancelled: string): string => {
    if (caught instanceof CeremonyCancelled) return cancelled;
    const failed = errorCode(caught);
    if (failed === 'pairing_unknown') setPairing(null);
    return error(failed).title;
  };
  const [nameInvalid, setNameInvalid] = useState(false);
  /** The account's own display name once read (existing account, unlocked); '' if none was found. */
  const [known, setKnown] = useState<string | null>(null);
  /** The name field of the existing account is open ("Use another name", or no name was found). */
  const [rename, setRename] = useState(false);
  /** The display name of the last accept (a retry sends the same). */
  const [sent, setSent] = useState('');

  useEffect(() => {
    let live = true;
    const verify = async () => {
      let next: OrgStep;
      try {
        const invitation = await openInvitation(code, lookup, verifiedPlatform(app.store));
        next = afterVerify(invitation, await settledPhase(app.store));
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

  /** The account's own name, read from its orgs (landing org first); '' if none. */
  const readOwnName = (): Promise<string> => {
    const { me } = app.store.getState();
    const landing = app.landingOrg();
    const orgs = meOrgs(me.profile).map((item) => item.org_id);
    const ordered = landing === null ? orgs : [landing, ...orgs.filter((org) => org !== landing)];
    return ownDisplayName(app.tasks, ordered, me.userId!);
  };

  const ownName = async (): Promise<string> => {
    const found = await readOwnName();
    setKnown(found);
    return found;
  };

  // the unlocked account's name for the join button ("Join <org> as <name>"), read once per account
  const ready = step.step === 'existing_account' && step.mode === 'ready';
  useEffect(() => {
    // a late answer after the screen is gone sets nothing (React ignores it)
    if (ready && known === null) void readOwnName().then(setKnown);
    // `readOwnName` reads the store at call time
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [ready, known]);

  /** The display name, or null (with the inline error) if it is not 1–80 characters. */
  const checkedName = (): string | null => {
    const checked = displayName(name);
    setNameInvalid(checked === null);
    return checked;
  };

  const joined = (invitation: OrgInvitation) => {
    noteJoined(app.store, { org: invitation.org, orgName: invitation.orgName });
    setStep({ step: 'done', org: invitation.org, orgName: invitation.orgName });
  };

  const isMember = async (org: string) => meOrgs(await app.loadMe()).some((item) => item.org_id === org);

  const accept = async (invitation: OrgInvitation, joiner: Joiner, memberName: string) => {
    setSent(memberName);
    setStep({ step: 'accept', invitation, failed: null });
    const request = (method: string, target: string, options?: { body: unknown }) =>
      app.client.request(method, target, options);
    try {
      const platform = verifiedPlatform(app.store) ?? (await loadPlatform(request, app.config.platform_root_key));
      const complete = await completeInvitation(invitation, code, platform);
      const result = await joinOrg(
        { anon: app.anon, request, isMember },
        { invitation: complete, joiner, displayName: memberName, platform, code },
      );
      await app.installOrg(result.prepared);
      // the display name waits for the current org key if the batch could not carry `member.profile`
      const profile = result.prepared.profile ? null : displayName(memberName);
      queueSelfWraps(app.store, result.prepared.org, joiner.userId, result.invitation.keys, profile);
      void app.tasks.refreshLog(result.prepared.org);
      joined(invitation);
    } catch (caught) {
      setStep(failedStep(caught) ?? { step: 'accept', invitation, failed: errorCode(caught) });
    }
  };

  // --- new user: the registration of WEB-03 (gesture 1, gesture 2; without PRF `prf_required`) ---

  const createPasskey = async (event: FormEvent, current: Step<'intro'>) => {
    event.preventDefault();
    if (!codeOk || checkedName() === null) return;
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
      const result = await signIn({ anon: app.anon, credentials, origin, ...tag });
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
      void accept(current.invitation, { userId, identity }, name);
    } catch (caught) {
      setStep({ ...current, busy: false, notice: signInNotice(caught, t('invite.unlock.cancelled')) });
    }
  };

  // --- existing account (04-crypto.md section 5.2a): sign in if needed, unlock, join – one action ---

  /**
   * Joins with the unlocked account: the typed name if the field is open, else the account's own name. `changed`: another
   * account than the one before signed in – nothing typed or read for the previous one is used.
   */
  const joinUnlocked = async (current: Step<'existing_account'>, changed = false) => {
    const { userId, identity } = app.store.getState().me;
    // already a member (the verified log says so): nothing to accept, the org opens
    if (current.invitation.log?.state.member(userId!)) {
      joined(current.invitation);
      return;
    }
    const typed = rename && !changed;
    let memberName: string | null;
    if (typed) memberName = checkedName();
    else memberName = displayName(proposedName ?? ((!changed && known) || (await ownName())));
    if (memberName === null) {
      // no name in any org of the account: the field is asked for, the user has not joined yet
      if (changed) setName('');
      setRename(true);
      setStep({
        ...current,
        mode: 'ready',
        signIn: null,
        busy: false,
        notice: null,
        needName: current.needName || !typed,
      });
      return;
    }
    void accept(current.invitation, { userId: userId!, identity: identity! }, memberName);
  };

  const unlocked = async (current: Step<'existing_account'>, result: SignIn, identity: Identity, changed: boolean) => {
    await app.unlock(identity);
    // unlocked by the device key: a PRF-capable passkey without slot gets its slot now
    await uploadMissingSlot(app, result, identity);
    await joinUnlocked({ ...current, mode: 'ready', signIn: null }, changed);
  };

  const signInAndJoin = async (current: Step<'existing_account'>) => {
    setStep({ ...current, busy: true, notice: null });
    const before = app.store.getState().me.userId;
    let result: SignIn;
    try {
      result = await signIn({ anon: app.anon, credentials, origin, ...tag });
      await app.begin(result);
    } catch (caught) {
      setStep({ ...current, busy: false, notice: signInNotice(caught, t('invite-org.existing.cancelled')) });
      return;
    }
    const changed = result.userId !== before;
    if (changed) setKnown(null);
    const recovery = { ...current, mode: 'recovery', signIn: result, busy: false, notice: null, changed } as const;
    const unlocking = await unlockSignIn(app, result);
    if (unlocking === null) {
      setStep(recovery);
      return;
    }
    try {
      await unlocked(current, result, unlocking, changed);
    } catch (caught) {
      const failed = errorCode(caught);
      setStep(failed === 'decrypt_failed' ? recovery : { ...current, busy: false, notice: error(failed).title });
    }
  };

  const join = (event: FormEvent, current: Step<'existing_account'>) => {
    event.preventDefault();
    if (current.mode === 'ready') {
      setStep({ ...current, busy: true, notice: null });
      void joinUnlocked(current);
    } else void signInAndJoin(current);
  };

  /** "I already have an account – sign in and join" from the intro. */
  const existingFromIntro = (invitation: OrgInvitation) => {
    const current = existingStep(invitation, 'sign_in') as Step<'existing_account'>;
    setStep(current);
    void signInAndJoin(current);
  };

  const retry = (current: Step<'accept'>) => {
    const { userId, identity } = app.store.getState().me;
    void accept(current.invitation, { userId: userId!, identity: identity! }, sent);
  };

  if (step.step === 'done') {
    if (pairing !== null) return <Navigate to="/connect" replace state={{ resume: pairing }} />;
    return <Navigate to={`/o/${step.org}`} replace />;
  }

  const orgName = (invitation: OrgInvitation) => invitation.orgName || t('invite-org.org-unnamed');

  const nameField = (disabled: boolean, label = t('invite.name.label')) =>
    proposedName !== null ? <ProposedName name={proposedName} /> : nameInput(disabled, label);

  const nameInput = (disabled: boolean, label: string) => (
    <>
      <label htmlFor="invite-display-name">{label}</label>
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
      <p role="alert" className="text-red-800" data-testid="invite.notice">
        {text} {t('invite-org.not-joined.short')}
      </p>
    );

  /** "You have not joined yet" – on every step before the join. */
  const notJoined = (invitation: OrgInvitation) => (
    <p className="rounded bg-amber-50 px-3 py-2 text-amber-900" data-testid="invite.not-joined">
      {t('invite-org.not-joined', { org: orgName(invitation) })}
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

  const joinLabel = (current: Step<'existing_account'>) => {
    const org = orgName(current.invitation);
    if (current.busy) return t('invite-org.join.working', { org });
    if (current.mode === 'ready' && known) return t('invite-org.join.as', { org, name: known });
    return current.mode === 'ready' ? t('invite-org.join.ready', { org }) : t('invite-org.join.sign-in', { org });
  };

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
        <section className="flex flex-col gap-3" data-testid="invite.org-intro">
          <h2 className="text-xl font-semibold" data-testid="invite.org-name">
            {t('invite-org.intro.title', { org: orgName(step.invitation) })}
          </h2>
          {summary(step.invitation)}
          {pairing !== null && (
            <AgentNotice pairing={pairing} code={agentCode} disabled={step.step === 'create_passkey'} />
          )}
          {notJoined(step.invitation)}
          <p className="font-medium" data-testid="invite.one-account">
            {t('invite-org.intro.one-account')}
          </p>
          <div className="flex flex-col gap-2 rounded border border-gray-300 p-3">
            <h3 className="font-semibold">{t('invite-org.intro.existing.title')}</h3>
            <p>{t('invite-org.intro.existing.explain')}</p>
            <button
              type="button"
              className={PRIMARY}
              disabled={step.step === 'create_passkey'}
              onClick={() => existingFromIntro(step.invitation)}
              data-testid="invite.existing-account"
            >
              {t('invite-org.existing')}
            </button>
          </div>
          <form
            className="flex flex-col gap-2 rounded border border-gray-300 p-3"
            onSubmit={(event) => step.step === 'intro' && void createPasskey(event, step)}
          >
            <h3 className="font-semibold">{t('invite-org.intro.new.title')}</h3>
            {nameField(step.step === 'create_passkey')}
            <p>{t('invite.create.explain')}</p>
            <button
              type="submit"
              className={PRIMARY}
              disabled={step.step === 'create_passkey' || !codeOk}
              data-testid="invite.create-passkey"
            >
              {step.step === 'create_passkey' ? t('invite.create.working') : t('invite-org.new')}
            </button>
          </form>
          {step.step === 'intro' && notice(step.notice)}
        </section>
      )}
      {step.step === 'unlock_passkey' && (
        <section className="flex flex-col gap-3" data-testid="invite.unlock-step">
          <h2 className="text-xl font-semibold">{t('invite.unlock.title')}</h2>
          {notJoined(step.invitation)}
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
          {pairing !== null && <AgentNotice pairing={pairing} code={agentCode} disabled={step.busy} />}
          {notJoined(step.invitation)}
          {step.mode === 'recovery' ? (
            <PrfMissing
              signIn={step.signIn!}
              unlock={(identity) => app.unlock(identity)}
              onUnlocked={() => void joinUnlocked({ ...step, mode: 'ready', signIn: null }, step.changed)}
            />
          ) : (
            <form className="flex flex-col gap-3" onSubmit={(event) => join(event, step)}>
              <p>{t(step.mode === 'ready' ? 'invite-org.existing.ready' : 'invite-org.existing.explain')}</p>
              {step.needName && (
                <p role="alert" data-testid="invite.need-name">
                  {t('invite-org.existing.need-name')}
                </p>
              )}
              {proposedName !== null && <ProposedName name={proposedName} />}
              {rename && nameField(step.busy, t('invite-org.existing.name'))}
              <button type="submit" className={PRIMARY} disabled={step.busy || !codeOk} data-testid="invite.join">
                {joinLabel(step)}
              </button>
              {!rename && proposedName === null && (
                <button
                  type="button"
                  className={LINK}
                  disabled={step.busy}
                  onClick={() => {
                    setName(known ?? '');
                    setRename(true);
                  }}
                  data-testid="invite.rename"
                >
                  {t('invite-org.existing.rename')}
                </button>
              )}
            </form>
          )}
          {step.mode === 'ready' && (
            <button
              type="button"
              className={LINK}
              disabled={step.busy}
              onClick={() => setStep(existingStep(step.invitation, 'sign_in'))}
              data-testid="invite.other-account"
            >
              {t('invite-org.existing.other-account')}
            </button>
          )}
          <button
            type="button"
            className={LINK}
            disabled={step.busy}
            onClick={() => setStep({ step: 'intro', invitation: step.invitation, notice: null })}
            data-testid="invite.new-account"
          >
            {t('invite-org.existing.new-account')}
          </button>
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
            {notJoined(step.invitation)}
            <p>{error(step.failed).title}</p>
            <button type="button" className={PRIMARY} onClick={() => retry(step)} data-testid="invite.accept-retry">
              {t('invite.accept.retry')}
            </button>
          </section>
        ))}
    </section>
  );
}
