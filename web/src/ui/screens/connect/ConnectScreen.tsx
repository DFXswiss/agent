// `/connect` (11-agent-access.md section 5.1, ADR-056 section 3): the user confirms the pairing of their own AI agent
// with one passkey gesture. The fragment is read once and removed; the hub's keys must match the link's fingerprint
// (`pairing_mismatch`) and the pairing must be open (`pairing_unknown`). The page shows the agent's name and computer
// as the link states them, the age of the request, that the agent gets full access, that it becomes the user's main
// orchestrator (concept idea 34: one at a time; confirming replaces the current one, which is disconnected at once),
// and the warning. "Confirm with passkey" runs the login ceremony tagged with the pairing, unlocks the identity (PRF;
// without it the guidance of `ui/prf-missing`) and loads the user's agents (`GET /v1/me/agents`): when there is a main
// orchestrator the user was not yet shown, the page names it ("Replaces “X”", label opened like the settings list) and
// asks once more before anything is sent. Then it seals identity and label and posts the confirmation;
// `pairing_gesture_required` (more than 300 s since the ceremony) offers the gesture again with the same confirmation
// body. Then the same tab hands over to the agent's loopback listener with a form POST. A tab that is already
// unlocked loads the agents at once, so the replaced agent is named before the gesture.
import { useEffect, useRef, useState } from 'react';
import { CeremonyCancelled } from '../../../core/account/ceremony';
import { type SignIn, signIn } from '../../../core/account/login';
import { type PairingLink, parsePairingLink } from '../../../core/agents/link';
import { loadAgents } from '../../../core/agents/list';
import {
  loopbackForm,
  lookupPairing,
  type Pairing,
  type PreparedPairing,
  preparePairing,
  submitPairing,
} from '../../../core/agents/pairing';
import type { Identity } from '../../../core/crypto/keys';
import { mainOrchestrator } from '../../../core/store/slices/agents';
import { useNow } from '../../countdown/useNow';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { unlockSignIn } from '../../prf-missing/device';
import { PrfMissing } from '../../prf-missing/PrfMissing';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import { takeConnectFragment } from './fragment';
import { submitHandover } from './handover';

type Status =
  | { readonly step: 'loading' }
  | { readonly step: 'invalid' }
  | { readonly step: 'error'; readonly code: string }
  | { readonly step: 'ready'; readonly notice: string | null }
  | { readonly step: 'working' }
  | { readonly step: 'recovery'; readonly signIn: SignIn }
  | { readonly step: 'replace'; readonly identity: Identity; readonly replaces: string }
  | { readonly step: 'handover' };

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
  const [link] = useState<PairingLink | null>(() => parsePairingLink(takeConnectFragment()));
  const [pairing, setPairing] = useState<Pairing | null>(null);
  const [status, setStatus] = useState<Status>(() => (link === null ? { step: 'invalid' } : { step: 'loading' }));
  const prepared = useRef<PreparedPairing | null>(null);
  const current = useStore((state) => mainOrchestrator(state.agents.entries, link?.pairingId), app.store);
  /** The main orchestrator the page showed as replaced when the user last confirmed (null: none shown). */
  const shown = useRef<string | null>(null);

  useEffect(() => {
    if (link === null) return;
    let live = true;
    lookupPairing(app.anon, link).then(
      (found) => {
        if (!live) return;
        setPairing(found);
        setStatus({ step: 'ready', notice: null });
        if (app.store.getState().me.identity !== null) void loadAgents(app.tasks);
      },
      (caught: unknown) => live && setStatus({ step: 'error', code: errorCode(caught) }),
    );
    return () => {
      live = false;
    };
  }, [app, link]);

  const fail = (caught: unknown) => {
    const code = errorCode(caught);
    if (code === 'pairing_gesture_required') setStatus({ step: 'ready', notice: t('connect.again') });
    else setStatus({ step: 'error', code });
  };

  const confirm = async (identity: Identity) => {
    const userId = app.userId!;
    try {
      prepared.current ??= await preparePairing(pairing!, link!, userId, identity);
      await submitPairing(app.client, prepared.current);
    } catch (caught) {
      fail(caught);
      return;
    }
    setStatus({ step: 'handover' });
    submitHandover(loopbackForm(link!.port, prepared.current, app.rootKey));
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
          <p role="note" className="rounded border border-amber-600 bg-amber-50 p-3" data-testid="connect.warning">
            {t('connect.warning')}
          </p>
        </section>
      )}
      {(status.step === 'ready' || status.step === 'working') && (
        <button
          type="button"
          className={BUTTON}
          onClick={() => void start()}
          disabled={status.step === 'working'}
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
      {status.step === 'handover' && (
        <p role="status" data-testid="connect.handover">
          {t('connect.handover')}
        </p>
      )}
    </main>
  );
}
