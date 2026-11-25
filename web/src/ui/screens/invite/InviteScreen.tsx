// `/invite#<code>` for platform invitations (06-web.md sections 3.1, 3.1.1, 3.2; 04-crypto.md sections 5.1, 5.3):
// parsing → lookup → intro (display name) → create_passkey (gesture 1, `register/begin` + `create()` +
// `register/finish`) → unlock_passkey (gesture 2: login ceremony, passkey slot upload with PRF) → accept (private
// genesis, `POST /v1/orgs`) → done (`/o/:org`). A passkey without PRF output ends in `prf_required`: the registration
// is withdrawn (account deleted, invitation free again) and "Try again with another passkey" returns to the intro
// (KOR-22, ADR-055). A cancelled gesture returns to the step before with "try again"; a failed accept retries with the identical request (same IDs, idempotent). Org invitations branch into
// `org/` (WEB-08).
//
// An invitation of the user's AI agent (11-agent-access.md section 5.2) carries a pairing after the code
// (`#<code>&p=…&f=…&c=…&a=…&h=…&u=<name>`): the pairing is looked up with the invitation (mismatch or expiry end in
// the invalid state), the page names the agent and warns as `/connect` does, `u` replaces the name field, and the sign-in
// after the new passkey is the pairing-tagged ceremony. When the account is ready, `/connect` takes over with the
// pairing (route state `resume`) and confirms with that session – the user creates the passkey and nothing else.
// Without a code in this tab (reloaded after the code left the address) the screen says so and that nothing was
// joined. A browser with a stored session is told before the intro that this link creates a second, separate
// account; it goes to its account or, deliberately, creates the new one (KOR-36).
// A pairing from another computer (`m=remote`, ADR-067 section 6) asks for the agent code before the passkey is
// created: "Create passkey" stays disabled until it matches, so a wrong or missing code creates no account.
import { type FormEvent, useEffect, useMemo, useState } from 'react';
import { Link, Navigate } from 'react-router-dom';
import { CeremonyCancelled } from '../../../core/account/ceremony';
import { createOrg, displayName, preparePrivateOrg } from '../../../core/account/genesis';
import { signIn } from '../../../core/account/login';
import { codeToInviteId, lookupInvite, register } from '../../../core/account/register';
import { parseInviteFragment } from '../../../core/agents/link';
import { lookupPairing } from '../../../core/agents/lookup';
import { formatEventTime } from '../../../core/i18n';
import { HubError } from '../../../core/net/signedFetch';
import { useStore } from '../../hooks/useStore';
import { parseTime } from '../../../core/encoding/time';
import { useI18n } from '../../i18n';
import { withdrawRegistration } from '../../prf-missing/device';
import { PrfRequired } from '../../prf-missing/PrfRequired';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import { useAgentCode } from '../connect/AgentCode';
import { AgentNotice, ProposedName } from './AgentNotice';
import { forgetFragment, takeFragment } from './fragment';
import { afterLookup, failedLookup, type Step } from './machine';
import { OrgInvite } from './org/OrgInvite';

const PRIMARY = 'min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60';

export function InviteScreen() {
  const { app, credentials, origin } = useAppEnv();
  const { t, error, locale } = useI18n();
  const [fragment] = useState(takeFragment);
  const invite = useMemo(() => parseInviteFragment(fragment), [fragment]);
  const { code } = invite;
  /** The agent's pairing; dropped when it expired before the sign-in (the account is then made without it). */
  const [pairing, setPairing] = useState(invite.pairing);
  const [step, setStep] = useState<Step>({ step: 'parsing' });
  const [name, setName] = useState(invite.displayName ?? '');
  const [nameInvalid, setNameInvalid] = useState(false);
  const [attempt, setAttempt] = useState(0);
  const phase = useStore((state) => state.session.phase, app.store);
  /** A signed-in browser chose to create a further account anyway. */
  const [anyway, setAnyway] = useState(false);
  /** The code the user must type for a remote pairing (from the hub's nonce), null otherwise. */
  const [expectedCode, setExpectedCode] = useState<string | null>(null);
  const agentCode = useAgentCode(expectedCode);
  const codeOk = pairing === null || agentCode.matches;

  // the code is in this screen's state now; the remembered copy of the first read is not needed any more
  useEffect(forgetFragment, []);

  useEffect(() => {
    let live = true;
    const run = async () => {
      if (code === '') {
        setStep({ step: 'missing' });
        return;
      }
      setStep({ step: 'parsing' });
      try {
        if (!invite.valid) throw new HubError('pairing_mismatch', 0);
        const id = await codeToInviteId(code);
        setStep({ step: 'lookup' });
        const lookup = await lookupInvite(app.anon, id);
        const found = invite.pairing === null ? null : await lookupPairing(app.anon, invite.pairing, origin);
        if (live) setExpectedCode(found?.agentCode ?? null);
        if (live) setStep(afterLookup(lookup));
      } catch (caught) {
        if (live) setStep(failedLookup(caught));
      }
    };
    void run();
    return () => {
      live = false;
    };
  }, [app, code, invite, attempt, origin]);

  const createPasskey = async (event: FormEvent, current: Extract<Step, { step: 'intro' }>) => {
    event.preventDefault();
    if (!codeOk) return;
    if (displayName(name) === null) {
      setNameInvalid(true);
      return;
    }
    setStep({ step: 'create_passkey', lookup: current.lookup });
    try {
      const registration = await register(app.anon, credentials, code);
      setStep({ step: 'unlock_passkey', lookup: current.lookup, registration, busy: false, notice: null });
    } catch (caught) {
      if (caught instanceof CeremonyCancelled) {
        setStep({ ...current, notice: t('invite.cancelled') });
        return;
      }
      const failed = failedLookup(caught);
      setStep(failed.step === 'invalid' ? failed : { ...current, notice: error(errorCode(caught)).title });
    }
  };

  const unlock = async (current: Extract<Step, { step: 'unlock_passkey' }>) => {
    setStep({ ...current, busy: true, notice: null });
    try {
      const tag = pairing === null ? {} : { pairingId: pairing.pairingId };
      const result = await signIn({ anon: app.anon, credentials, origin, ...tag });
      if (result.userId !== current.registration.userId) {
        setStep({ ...current, busy: false, notice: t('invite.unlock.other-account') });
        return;
      }
      await app.begin(result);
      await app.unlock(current.registration.identity);
      const uploaded = await app.uploadPasskeySlot(result, current.registration.identity);
      if (!uploaded) {
        setStep({ step: 'prf_required', lookup: current.lookup, released: await withdrawRegistration(app) });
        return;
      }
      app.notePrf(result, uploaded);
      void accept({
        step: 'accept',
        lookup: current.lookup,
        registration: current.registration,
        prepared: null,
        failed: null,
      });
    } catch (caught) {
      const code = errorCode(caught);
      // the agent's request expired meanwhile: the next sign-in finishes the account without it
      if (code === 'pairing_unknown') setPairing(null);
      const notice = caught instanceof CeremonyCancelled ? t('invite.unlock.cancelled') : error(code).title;
      setStep({ ...current, busy: false, notice });
    }
  };

  const accept = async (current: Extract<Step, { step: 'accept' }>) => {
    const base = { step: 'accept', lookup: current.lookup, registration: current.registration } as const;
    let prepared = current.prepared;
    setStep({ ...base, prepared, failed: null });
    try {
      prepared ??= await preparePrivateOrg({
        userId: current.registration.userId,
        identity: current.registration.identity,
        displayName: name,
        locale,
        platformInviteId: current.lookup.inviteId,
      });
      await createOrg((method, target, options) => app.client.request(method, target, options), prepared);
      await app.installOrg(prepared);
      setStep({ step: 'done', org: prepared.org });
    } catch (caught) {
      setStep({ ...base, prepared, failed: errorCode(caught) });
    }
  };

  if (step.step === 'done') {
    if (pairing !== null) return <Navigate to="/connect" replace state={{ resume: pairing }} />;
    return <Navigate to={`/o/${step.org}`} replace />;
  }

  return (
    <main className="mx-auto flex max-w-xl flex-col gap-4 p-6" data-testid="invite.page" data-step={step.step}>
      <h1 className="text-2xl font-semibold">{t('invite.title')}</h1>
      {(step.step === 'parsing' || step.step === 'lookup') && <p data-testid="invite.lookup">{t('invite.lookup')}</p>}
      {step.step === 'invalid' && (
        <section role="alert" data-testid="invite.invalid" data-code={step.code}>
          <h2 className="text-xl font-semibold">{error(step.code).title}</h2>
          <p>{error(step.code).body}</p>
          <p>{error(step.code).action}</p>
        </section>
      )}
      {step.step === 'missing' && (
        <section role="alert" data-testid="invite.missing">
          <h2 className="text-xl font-semibold">{t('invite.missing.title')}</h2>
          <p>{t('invite.missing.body')}</p>
        </section>
      )}
      {step.step === 'malformed' && (
        <section role="alert" data-testid="invite.malformed" data-code={step.code}>
          <h2 className="text-xl font-semibold">{t('invite.check-code')}</h2>
          <p>{error(step.code).body}</p>
          <p>{t('invite.check-code.body')}</p>
        </section>
      )}
      {step.step === 'error' && (
        <section role="alert" className="flex flex-col gap-3" data-testid="invite.error" data-code={step.code}>
          <p>{error(step.code).title}</p>
          <button type="button" className={PRIMARY} onClick={() => setAttempt((n) => n + 1)} data-testid="invite.retry">
            {t('invite.retry')}
          </button>
        </section>
      )}
      {step.step === 'org' && (
        <OrgInvite
          code={code}
          lookup={step.lookup}
          pairing={pairing}
          agentCode={expectedCode}
          proposedName={invite.displayName}
        />
      )}
      {step.step === 'intro' && phase === 'starting' && <p data-testid="invite.lookup">{t('invite.lookup')}</p>}
      {step.step === 'intro' && (phase === 'locked' || phase === 'unlocked') && !anyway && (
        <section className="flex flex-col gap-3" data-testid="invite.has-account">
          <h2 className="text-xl font-semibold">{t('invite.has-account.title')}</h2>
          <p>{t('invite.has-account.body')}</p>
          <Link className={`${PRIMARY} flex items-center justify-center`} to="/" data-testid="invite.to-account">
            {t('invite.has-account.open')}
          </Link>
          <button
            type="button"
            className="min-h-11 self-start underline"
            onClick={() => setAnyway(true)}
            data-testid="invite.new-anyway"
          >
            {t('invite.has-account.anyway')}
          </button>
        </section>
      )}
      {(step.step === 'create_passkey' || (step.step === 'intro' && (phase === 'anonymous' || anyway))) && (
        <form
          className="flex flex-col gap-3"
          onSubmit={(event) => step.step === 'intro' && void createPasskey(event, step)}
        >
          <h2 className="text-xl font-semibold">{t('invite.intro.title')}</h2>
          <p>{t('invite.intro.platform')}</p>
          <p data-testid="invite.one-account">
            {t('invite.intro.one-account')}{' '}
            <Link className="underline" to="/login" data-testid="invite.sign-in-instead">
              {t('invite.intro.sign-in')}
            </Link>
          </p>
          <p className="text-sm text-gray-700" data-testid="invite.expires">
            {t('invite.intro.expires', {
              time: formatEventTime(parseTime(step.lookup.expiresAt), {
                locale,
                timeZone: Intl.DateTimeFormat().resolvedOptions().timeZone,
                // an expiry always shows its date
                now: 0,
              }),
            })}
          </p>
          {pairing !== null && (
            <AgentNotice pairing={pairing} code={agentCode} disabled={step.step === 'create_passkey'} />
          )}
          {invite.displayName !== null ? (
            <ProposedName name={invite.displayName} />
          ) : (
            <>
              <label htmlFor="invite-display-name">{t('invite.name.label')}</label>
              <input
                id="invite-display-name"
                className="min-h-11 rounded border border-gray-400 px-3"
                autoComplete="name"
                maxLength={200}
                value={name}
                aria-invalid={nameInvalid}
                aria-describedby="invite-display-name-hint"
                onChange={(event) => {
                  setName(event.target.value);
                  setNameInvalid(false);
                }}
                disabled={step.step === 'create_passkey'}
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
          )}
          <p>{t('invite.create.explain')}</p>
          <button
            type="submit"
            className={PRIMARY}
            disabled={step.step === 'create_passkey' || !codeOk}
            data-testid="invite.create-passkey"
          >
            {step.step === 'create_passkey' ? t('invite.create.working') : t('invite.create-passkey')}
          </button>
          {step.step === 'intro' && step.notice !== null && (
            <p role="alert" data-testid="invite.notice">
              {step.notice}
            </p>
          )}
        </form>
      )}
      {step.step === 'unlock_passkey' && (
        <section className="flex flex-col gap-3" data-testid="invite.unlock-step">
          <h2 className="text-xl font-semibold">{t('invite.unlock.title')}</h2>
          <p>{t('invite.unlock.explain')}</p>
          <button
            type="button"
            className={PRIMARY}
            onClick={() => void unlock(step)}
            disabled={step.busy}
            data-testid="invite.unlock"
          >
            {step.busy ? t('invite.unlock.working') : t('invite.unlock')}
          </button>
          {step.notice !== null && (
            <p role="alert" data-testid="invite.notice">
              {step.notice}
            </p>
          )}
        </section>
      )}
      {step.step === 'prf_required' && (
        <PrfRequired
          context="register"
          released={step.released}
          onRetry={() => setStep({ step: 'intro', lookup: step.lookup, notice: null })}
        />
      )}
      {step.step === 'accept' &&
        (step.failed === null ? (
          <p data-testid="invite.accepting">{t('invite.accept.working')}</p>
        ) : (
          <section
            role="alert"
            className="flex flex-col gap-3"
            data-testid="invite.accept-failed"
            data-code={step.failed}
          >
            <p>{error(step.failed).title}</p>
            <button
              type="button"
              className={PRIMARY}
              onClick={() => void accept(step)}
              data-testid="invite.accept-retry"
            >
              {t('invite.accept.retry')}
            </button>
          </section>
        ))}
    </main>
  );
}
