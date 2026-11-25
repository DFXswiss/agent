// The dialog "Make this session my main orchestrator" (11-agent-access.md section 11, ADR-082 sections 6.3 and 9.8).
// On opening it asks the session's runner for keys (`requestOrchestrator`) and waits for the
// pairing (`waitForPairing`), then checks in the verified log that the organization is the user's private one and the
// device's signature over the keys' fingerprint under the device key of
// the verified platform log (`checkSessionPairing`; else `pairing_mismatch`). It names the session, its device, the
// main orchestrator it replaces ("Replaces “X”", from the user's agent list) and says that the session gets full
// access and that its key stays inside the session on DFX hardware and ends with it. "Confirm with passkey" runs the
// login ceremony tagged with the pairing, unlocks the identity (without PRF: `prf_missing`), confirms exactly like a
// connected agent (`preparePairing`, `confirmPairing`: certificate, one batch per organization with the replacement,
// credential-key wraps) and then sends the signed handover (`handOver`). It waits for the session's claim by reading
// the agent list every `CLAIM_POLL_MS` (connected / failed). `pairing_gesture_required` and a cancelled ceremony offer
// the gesture again with the same confirmation values.
import { useEffect, useRef, useState } from 'react';
import { CeremonyCancelled } from '../../../core/account/ceremony';
import { signIn } from '../../../core/account/login';
import { loadAgents } from '../../../core/agents/list';
import {
  checkSessionPairing,
  handOver,
  requestOrchestrator,
  sessionLabel,
  waitForPairing,
} from '../../../core/agents/orchestrator';
import type { Pairing } from '../../../core/agents/lookup';
import {
  confirmPairing,
  pairingGen,
  pairingOutcome,
  type PreparedPairing,
  preparePairing,
} from '../../../core/agents/pairing';
import type { Identity } from '../../../core/crypto/keys';
import { HubError } from '../../../core/net/signedFetch';
import { verifiedPlatform } from '../../../core/org/batch';
import { mainOrchestrator } from '../../../core/store/slices/agents';
import { orgEntry } from '../../../core/store/slices/orgs';
import type { SessionEntry } from '../../../core/store/slices/tasks';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { unlockSignIn } from '../../prf-missing/device';
import { useAppEnv } from '../../shell/context';
import { errorCode } from '../../shell/errors';
import { Dialog } from '../members/Dialog';
import { useDeviceNames } from '../devices/labels';

/** How often the dialog reads the agent list while the session claims. */
export const CLAIM_POLL_MS = 3_000;
const BUTTON = 'min-h-11 rounded bg-gray-900 px-4 text-white disabled:opacity-60';
const SECONDARY = 'min-h-11 rounded border border-gray-400 px-4';

type Status =
  | { readonly step: 'preparing' }
  | { readonly step: 'ready'; readonly notice: string | null }
  | { readonly step: 'working' }
  | { readonly step: 'error'; readonly code: string }
  | { readonly step: 'handover'; readonly outcome: 'waiting' | 'connected' | 'failed' };

const sleep = (ms: number) => new Promise<void>((resolve) => setTimeout(resolve, ms));

export default function OrchestratorDialog({
  entry,
  onClose,
}: {
  readonly entry: SessionEntry;
  readonly onClose: () => void;
}) {
  const { app, credentials, origin } = useAppEnv();
  const { t, error } = useI18n();
  const { session_id: session, org_id: org, device_id: device } = entry.row;
  const name = useDeviceNames([org]);
  const deviceName = device === null ? null : name(device);
  const [status, setStatus] = useState<Status>({ step: 'preparing' });
  const pairing = useRef<Pairing | null>(null);
  const prepared = useRef<PreparedPairing | null>(null);
  const live = useRef(true);
  const replaces = useStore((state) => mainOrchestrator(state.agents.entries, pairing.current?.pairingId), app.store);
  const waiting = status.step === 'handover' && status.outcome === 'waiting';

  // ask the runner for keys and check them (module comment)
  useEffect(() => {
    live.current = true;
    const prepare = async () => {
      if (app.store.getState().me.identity !== null) void loadAgents(app.tasks);
      const asked = await requestOrchestrator(app.client, session);
      const view = await waitForPairing(app.client, asked, { now: Date.now, sleep, live: () => live.current });
      const state = orgEntry(app.store.getState().orgs, org).log?.state ?? null;
      pairing.current = await checkSessionPairing(view, verifiedPlatform(app.store), view.pairing!, state, app.userId!);
    };
    prepare().then(
      () => live.current && setStatus({ step: 'ready', notice: null }),
      (caught: unknown) => live.current && setStatus({ step: 'error', code: errorCode(caught) }),
    );
    return () => {
      live.current = false;
    };
  }, [app, session, org]);

  // the session claims with the handover; the agent list says when it did
  useEffect(() => {
    if (!waiting) return;
    const poll = async () => {
      await loadAgents(app.tasks);
      const outcome = pairingOutcome(app.store.getState().agents.entries, pairing.current!.pairingId);
      if (outcome !== 'waiting') setStatus({ step: 'handover', outcome });
    };
    void poll();
    const timer = setInterval(() => void poll(), CLAIM_POLL_MS);
    return () => clearInterval(timer);
  }, [app, waiting]);

  const fail = (caught: unknown) => {
    const code = errorCode(caught);
    if (code === 'pairing_gesture_required') setStatus({ step: 'ready', notice: t('session.orchestrator.again') });
    else if (caught instanceof CeremonyCancelled)
      setStatus({ step: 'ready', notice: t('session.orchestrator.cancelled') });
    else setStatus({ step: 'error', code });
  };

  const unlocked = async (): Promise<Identity> => {
    const result = await signIn({ anon: app.anon, credentials, origin, pairingId: pairing.current!.pairingId });
    await app.begin(result);
    const held = app.store.getState().me.identity;
    if (held !== null) return held;
    const identity = await unlockSignIn(app, result);
    if (identity === null) throw new HubError('prf_missing', 0);
    await app.unlock(identity);
    return identity;
  };

  const confirm = async () => {
    setStatus({ step: 'working' });
    const found = pairing.current!;
    try {
      const identity = await unlocked();
      if (prepared.current === null) {
        const gen = await pairingGen(app.client, found.pairingId);
        const label = sessionLabel(deviceName, t('session.orchestrator.label'));
        prepared.current = await preparePairing(found, label, app.userId!, identity, app.rootKey, gen);
      }
      await confirmPairing({ ...app.tasks, prefs: app.prefs }, prepared.current);
      await handOver(app.client, prepared.current, found, session, identity, app.rootKey);
    } catch (caught) {
      fail(caught);
      return;
    }
    setStatus({ step: 'handover', outcome: 'waiting' });
  };

  const busy = status.step === 'working';
  return (
    <Dialog title={t('session.orchestrator.title')} testId="orchestrator.dialog" busy={busy} onClose={onClose}>
      <dl className="grid grid-cols-[auto_1fr] gap-x-3 gap-y-1">
        <dt className="text-gray-700">{t('session.orchestrator.session')}</dt>
        <dd className="break-words font-medium" data-testid="orchestrator.session">
          {t('session.orchestrator.this')}
        </dd>
        <dt className="text-gray-700">{t('session.orchestrator.device')}</dt>
        <dd className="break-words font-medium" data-testid="orchestrator.device">
          {deviceName ?? t('task.sessions.device.none')}
        </dd>
      </dl>
      <p className="font-semibold" data-testid="orchestrator.access">
        {t('session.orchestrator.access')}
      </p>
      <p data-testid="orchestrator.main">{t('session.orchestrator.main')}</p>
      <p className="text-sm text-gray-700" data-testid="orchestrator.key">
        {t('session.orchestrator.key')}
      </p>
      {replaces !== null && (
        <p className="font-semibold" data-testid="orchestrator.replaces" data-agent={replaces.agentId}>
          {replaces.label === null
            ? t('session.orchestrator.replaces-unknown')
            : t('session.orchestrator.replaces', { name: replaces.label.name })}
        </p>
      )}
      {status.step === 'preparing' && (
        <p role="status" data-testid="orchestrator.preparing">
          {t('session.orchestrator.preparing')}
        </p>
      )}
      {status.step === 'error' && (
        <div role="alert" className="flex flex-col gap-1" data-testid="orchestrator.error" data-code={status.code}>
          <p className="font-semibold">{error(status.code).title}</p>
          <p>{error(status.code).body}</p>
          <p>{error(status.code).action}</p>
        </div>
      )}
      {status.step === 'ready' && status.notice !== null && (
        <p role="alert" data-testid="orchestrator.notice">
          {status.notice}
        </p>
      )}
      {status.step === 'handover' && (
        <p role="status" data-testid={`orchestrator.outcome.${status.outcome}`}>
          {t(`session.orchestrator.outcome.${status.outcome}`)}
        </p>
      )}
      <div className="flex flex-wrap gap-2">
        {(status.step === 'ready' || status.step === 'working' || status.step === 'preparing') && (
          <button
            type="button"
            className={BUTTON}
            onClick={() => void confirm()}
            disabled={status.step !== 'ready'}
            data-testid="orchestrator.confirm"
          >
            {busy ? t('session.orchestrator.working') : t('session.orchestrator.confirm')}
          </button>
        )}
        <button type="button" className={SECONDARY} onClick={onClose} disabled={busy} data-testid="orchestrator.close">
          {status.step === 'handover' ? t('session.orchestrator.done') : t('session.orchestrator.cancel')}
        </button>
      </div>
    </Dialog>
  );
}
