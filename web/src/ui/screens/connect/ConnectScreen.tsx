// `/connect` (11-agent-access.md section 5.1, ADR-056 section 3): the user confirms the pairing of their own AI agent
// with one passkey gesture. The fragment is read once and removed; the hub's keys must match the link's fingerprint
// (`pairing_mismatch`) and the pairing must be open (`pairing_unknown`). The page shows the agent's name and computer
// as the link states them, the age of the request, that the agent gets full access, that it becomes the user's main
// orchestrator (concept idea 34: one at a time; confirming replaces the current one, which is disconnected at once),
// and the warning. "Confirm with passkey" runs the login ceremony tagged with the pairing, unlocks the identity (PRF;
// without it the guidance of `ui/prf-missing`) and loads the user's agents (`GET /v1/me/agents`): when there is a main
// orchestrator the user was not yet shown, the page names it ("Replaces “X”", label opened like the settings list) and
// asks once more before anything is sent. Then it reads the generation the hub counts for the user, signs the agent's
// certificate, seals the user's public keys for the agent and the label for the user, and posts the confirmation with
// one batch per organization (the agent added, a replaced orchestrator removed with its rotations, ADR-082 section 2)
// and the credential-key wraps; the user's credential key is rotated when the logs owe it (`confirmPairing`).
// `pairing_gesture_required` (more than 300 s since the ceremony) offers the gesture again with the same fixed
// confirmation values; `client_outdated` asks to reload. Then the same tab hands over to the agent's loopback listener
// with a form POST. A tab that is already
// unlocked loads the agents at once, so the replaced agent is named before the gesture.
//
// After an invitation with pairing (11-agent-access.md section 5.2) the invitation screen navigates here with the
// pairing in the route state (`resume`) instead of a fragment: the account was just made or joined with the
// pairing-tagged sign-in, so the page confirms at once with the unlocked identity – no second gesture unless the hub
// asks for one (`pairing_gesture_required`) or a main orchestrator is to be replaced.
//
// A pairing from another computer (`m=remote`, ADR-067): the link's mode must agree with the hub's (else "This link
// is not valid"), the page shows the warning of section 4.2 and asks for the agent code; "Confirm with passkey" stays
// disabled until the typed code equals the one computed from the link and the hub's nonce (the page never shows it).
// The confirmation carries the handover sealed to the agent instead of the loopback form; then the page shows the
// return code ("Give this code to your AI agent") and waits for the agent's claim, reading `GET /v1/me/agents` every
// `POLL_MS` until the new row is active (connected) or expired, revoked or gone (failed). After an invitation the
// code was asked there, before the passkey was created, so the resumed page does not ask again.
import { useEffect, useRef, useState } from 'react';
import { useLocation } from 'react-router-dom';
import { CeremonyCancelled } from '../../../core/account/ceremony';
import { type SignIn, signIn } from '../../../core/account/login';
import { type PairingLink, pairingFragment, parsePairingLink } from '../../../core/agents/link';
import { loadAgents } from '../../../core/agents/list';
import {
  confirmPairing,
  loopbackForm,
  lookupPairing,
  type Pairing,
  pairingGen,
  pairingOutcome,
  type PreparedPairing,
  preparePairing,
} from '../../../core/agents/pairing';
import { formatCode } from '../../../core/crypto/agent';
import type { Identity } from '../../../core/crypto/keys';
import { isRecord } from '../../../core/registry';
import { mainOrchestrator } from '../../../core/store/slices/agents';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { unlockSignIn } from '../../prf-missing/device';
import { PrfMissing } from '../../prf-missing/PrfMissing';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import { AgentCodeField, RemoteWarning, useAgentCode } from './AgentCode';
import { takeConnectFragment } from './fragment';
import { RequesterMessage } from './RequesterMessage';
import { submitHandover } from './handover';

type Status =
  | { readonly step: 'loading' }
  | { readonly step: 'invalid' }
  | { readonly step: 'error'; readonly code: string }
  | { readonly step: 'ready'; readonly notice: string | null }
  | { readonly step: 'working' }
  | { readonly step: 'recovery'; readonly signIn: SignIn }
  | { readonly step: 'replace'; readonly identity: Identity; readonly replaces: string }
  | { readonly step: 'handover' }
  | { readonly step: 'returned'; readonly code: string; readonly outcome: 'waiting' | 'connected' | 'failed' };

/** How often the page reads the agent list while it waits for a remote agent's claim. */
export const POLL_MS = 3_000;

/** The pairing the invitation screen hands over in the route state, or null. */
function resumedPairing(state: unknown): PairingLink | null {
  if (!isRecord(state) || !isRecord(state.resume)) return null;
  return parsePairingLink(pairingFragment(state.resume as unknown as PairingLink));
}

const READY: Status = { step: 'ready', notice: null };
type Returned = Extract<Status, { step: 'returned' }>;

const BUTTON = 'min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60';
const SECONDARY = 'min-h-11 rounded border border-gray-400 px-4';
const MINUTE = 60_000;

function Age({ requestedAt }: { readonly requestedAt: number }) {
  const { t } = useI18n();
  const minutes = Math.floor(Math.max(0, useNow(true, 10_000) - requestedAt) / MINUTE);
  return (
    <p className="text-sm text-gray-700" data-testid="connect.age" data-minutes={minutes}>
      {minutes === 0 ? t('connect.age.now') : t('connect.age.minutes', { minutes })}
    </p>
  );
}

export function ConnectScreen() {
  const { app, credentials, origin } = useAppEnv();
  const { t, error } = useI18n();
  const resume = resumedPairing(useLocation().state);
  const [link] = useState<PairingLink | null>(() => resume ?? parsePairingLink(takeConnectFragment()));
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [status, setStatus] = useState<Status>(() => (link === null ? { step: 'invalid' } : { step: 'loading' }));
  const prepared = useRef<PreparedPairing | null>(null);
  const current = useStore((state) => mainOrchestrator(state.agents.entries, link?.pairingId), app.store);
  /** The main orchestrator the page showed as replaced when the user last confirmed (null: none shown). */
  const shown = useRef<string | null>(null);
  // after an invitation the code was typed there, before the passkey was created
  const agentCode = useAgentCode(resume === null ? (pairing?.agentCode ?? null) : null);
  const waiting = status.step === 'returned' && status.outcome === 'waiting';

  // a remote agent claims within 10 minutes; the page reads the user's agents until the row says what happened
  useEffect(() => {
    if (!waiting) return;
    const poll = async () => {
      await loadAgents(app.tasks);
      const outcome = pairingOutcome(app.store.getState().agents.entries, link!.pairingId);
      if (outcome !== 'waiting') setStatus((current) => ({ ...(current as Returned), outcome }));
    };
    void poll();
    const timer = setInterval(() => void poll(), POLL_MS);
    return () => clearInterval(timer);
  }, [app, link, waiting]);

  useEffect(() => {
    if (link === null) return;
    let live = true;
    lookupPairing(app.anon, link, origin).then(
      (found) => {
        if (!live) return;
        setPairing(found);
        setStatus(resume !== null && app.store.getState().me.identity !== null ? { step: 'working' } : READY);
        if (app.store.getState().me.identity !== null) void loadAgents(app.tasks);
      },
      (caught: unknown) => live && setStatus({ step: 'error', code: errorCode(caught) }),
    );
    return () => {
      live = false;
    };
    // `resume` belongs to the first render (the link is taken from it once)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [app, link]);

  const fail = (caught: unknown) => {
    const code = errorCode(caught);
    if (code === 'pairing_gesture_required') setStatus({ step: 'ready', notice: t('connect.again') });
    else setStatus({ step: 'error', code });
  };

  const confirm = async (identity: Identity) => {
    const userId = app.userId!;
    try {
      if (prepared.current === null) {
        const gen = await pairingGen(app.client, link!.pairingId);
        prepared.current = await preparePairing(pairing!, link!, userId, identity, app.rootKey, gen);
      }
      await confirmPairing({ ...app.tasks, prefs: app.prefs }, prepared.current);
    } catch (caught) {
      fail(caught);
      return;
    }
    if (prepared.current.returnCode !== null) {
      setStatus({ step: 'returned', code: prepared.current.returnCode, outcome: 'waiting' });
      return;
    }
    setStatus({ step: 'handover' });
    submitHandover(loopbackForm(link!.port!, prepared.current, app.rootKey));
  };

  // the user's current main orchestrator, fresh from the hub: one not yet shown is named and asked about first
  const check = async (identity: Identity) => {
    await loadAgents(app.tasks);
    const agents = app.store.getState().agents;
    if (agents.status === 'error') {
      setStatus({ step: 'error', code: agents.code! });
      return;
    }
    const main = mainOrchestrator(agents.entries, link!.pairingId);
    if (main !== null && main.agentId !== shown.current) {
      setStatus({ step: 'replace', identity, replaces: main.agentId });
      return;
    }
    await confirm(identity);
  };

  // after an invitation with pairing: confirm with the identity the invitation unlocked, once the pairing is checked
  const resumed = useRef(false);
  useEffect(() => {
    const held = app.store.getState().me.identity;
    if (resume === null || pairing === null || held === null || resumed.current) return;
    resumed.current = true;
    void check(held);
  });

  const replace = (identity: Identity, replaces: string) => {
    shown.current = replaces;
    setStatus({ step: 'working' });
    void confirm(identity);
  };

  const start = async () => {
    shown.current = current?.agentId ?? null;
    setStatus({ step: 'working' });
    let result: SignIn;
    try {
      result = await signIn({ anon: app.anon, credentials, origin, pairingId: link!.pairingId });
      await app.begin(result);
    } catch (caught) {
      if (caught instanceof CeremonyCancelled) setStatus({ step: 'ready', notice: t('connect.cancelled') });
      else fail(caught);
      return;
    }
    // a repeated gesture of the same user keeps the identity already unlocked in this tab
    const held = app.store.getState().me.identity;
    if (held !== null) {
      await check(held);
      return;
    }
    const identity = await unlockSignIn(app, result);
    if (identity === null) {
      setStatus({ step: 'recovery', signIn: result });
      return;
    }
    try {
      await app.unlock(identity);
    } catch (caught) {
      if (errorCode(caught) === 'decrypt_failed') setStatus({ step: 'recovery', signIn: result });
      else fail(caught);
      return;
    }
    await check(identity);
  };

  return (
    <main className="mx-auto flex max-w-md flex-col gap-4 p-6" data-testid="connect.page">
      <h1 className="text-2xl font-semibold">{t('connect.title')}</h1>
      {status.step === 'loading' && <p data-testid="connect.loading">{t('connect.loading')}</p>}
      {status.step === 'invalid' && (
        <div role="alert" className="flex flex-col gap-2" data-testid="connect.invalid">
          <p className="font-semibold">{t('connect.invalid.title')}</p>
          <p>{t('connect.invalid.body')}</p>
        </div>
      )}
      {status.step === 'error' && (
        <div role="alert" className="flex flex-col gap-2" data-testid="connect.error" data-code={status.code}>
          <p className="font-semibold">{error(status.code).title}</p>
          <p>{error(status.code).body}</p>
          <p>{error(status.code).action}</p>
        </div>
      )}
      {link !== null && pairing !== null && status.step !== 'error' && (
        <section className="flex flex-col gap-3" data-testid="connect.request">
          <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
            <dt className="text-gray-700">{t('connect.agent')}</dt>
            <dd className="break-words font-medium" data-testid="connect.name">
              {link.name}
            </dd>
            <dt className="text-gray-700">{t('connect.host')}</dt>
            <dd className="break-words font-medium" data-testid="connect.host">
              {link.host}
            </dd>
          </dl>
          <p className="text-sm text-gray-700">{t('connect.stated')}</p>
          <RequesterMessage message={link.message} caption={t('connect.message')} testId="connect.message" />
          <Age requestedAt={pairing.requestedAt} />
          <p className="font-semibold" data-testid="connect.access">
            {t('connect.access')}
          </p>
          <p data-testid="connect.main">{t('connect.main')}</p>
          {current !== null && (
            <p className="font-semibold" data-testid="connect.replaces" data-agent={current.agentId}>
              {current.label === null
                ? t('connect.replaces-unknown')
                : t('connect.replaces', { name: current.label.name })}
            </p>
          )}
          {pairing.remote ? (
            <RemoteWarning host={link.host} testId="connect.remote-warning" />
          ) : (
            <p role="note" className="rounded border border-amber-600 bg-amber-50 p-3" data-testid="connect.warning">
              {t('connect.warning')}
            </p>
          )}
          {pairing.agentCode !== null && resume === null && (status.step === 'ready' || status.step === 'working') && (
            <AgentCodeField
              typed={agentCode.typed}
              onChange={agentCode.setTyped}
              matches={agentCode.matches}
              disabled={status.step === 'working'}
              testId="connect.code"
            />
          )}
        </section>
      )}
      {(status.step === 'ready' || status.step === 'working') && (
        <button
          type="button"
          className={BUTTON}
          onClick={() => void start()}
          disabled={status.step === 'working' || !agentCode.matches}
          data-testid="connect.confirm"
        >
          {status.step === 'working' ? t('connect.working') : t('connect.confirm')}
        </button>
      )}
      {status.step === 'ready' && status.notice !== null && (
        <p role="alert" data-testid="connect.notice">
          {status.notice}
        </p>
      )}
      {status.step === 'recovery' && (
        <PrfMissing
          signIn={status.signIn}
          unlock={(identity) => app.unlock(identity)}
          onUnlocked={() => void check(app.store.getState().me.identity!)}
        />
      )}
      {status.step === 'replace' && (
        <div role="alert" className="flex flex-col gap-2" data-testid="connect.replace">
          <p>{t('connect.replace.prompt')}</p>
          <div className="flex flex-wrap gap-2">
            <button
              type="button"
              className={BUTTON}
              onClick={() => replace(status.identity, status.replaces)}
              data-testid="connect.replace.confirm"
            >
              {t('connect.replace.confirm')}
            </button>
            <button
              type="button"
              className={SECONDARY}
              onClick={() => setStatus({ step: 'ready', notice: t('connect.cancelled') })}
              data-testid="connect.replace.cancel"
            >
              {t('connect.replace.cancel')}
            </button>
          </div>
        </div>
      )}
      {status.step === 'returned' && (
        <section
          className="flex flex-col gap-2 rounded border-2 border-gray-900 p-4"
          data-testid="connect.returned"
          data-outcome={status.outcome}
        >
          <p className="font-semibold">{t('connect.remote.return.title')}</p>
          <p className="font-mono text-3xl tracking-widest" data-testid="connect.return-code">
            {formatCode(status.code)}
          </p>
          <p>{t('connect.remote.return.body')}</p>
          <p role="status" data-testid={`connect.outcome.${status.outcome}`}>
            {t(`connect.remote.outcome.${status.outcome}`)}
          </p>
        </section>
      )}
      {status.step === 'handover' && (
        <p role="status" data-testid="connect.handover">
          {t('connect.handover')}
        </p>
      )}
    </main>
  );
}
