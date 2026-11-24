// The connect flow of the GitHub section (06-web.md section 8.1): the execution device (automatic assignment for a
// fresh org, then the eligible devices with the online one of the default workgroup preselected), the `github.connect`
// device request, the user code large with "Copy", the verification link (new tab) and a countdown until the code
// expires, then the GitHub login or the error. Responses are pulled on `device_response` frames and on a timer
// (`core/requests/watch.ts`); nothing confidential is shown (the token never reaches the browser UI).
//
// With `method="token"` (KOR-25, ADR-065) the same flow stores an existing token instead: next to the device choice
// a password-like field (no autocomplete, no spell check, outside any form a password manager would offer to save).
// Its value is read once when the user sends it, the field is emptied at once, and the token goes only into the request
// sealed with HPKE to the device – never into the store, the URL, a log or the component state. The device checks it at
// GitHub and answers `done` (login, scopes, source) or a failure with its own text (`github.token.failure.<code>`).
import { type RefObject, useEffect, useId, useRef, useState } from 'react';
import {
  connectDevices,
  connectProgress,
  type ConnectProgress,
  type DeviceChoice,
  startConnect,
  tokenInput,
} from '../../../../core/github/connection';
import { type PreparedRequest, requestErrorCode, type RequestView } from '../../../../core/requests/client';
import { watchRequest } from '../../../../core/requests/watch';
import { LiveRegion } from '../../../a11y/LiveRegion';
import { useNow } from '../../../countdown/useNow';
import { useI18n } from '../../../i18n';
import { useProfileDeviceNames } from '../../../screens/devices/labels';
import { useAppEnv } from '../../../shell/context';
import { useReason } from './reason';

type Phase =
  | { readonly kind: 'devices' }
  | { readonly kind: 'choose'; readonly devices: readonly DeviceChoice[]; readonly selected: string }
  | { readonly kind: 'none' }
  | { readonly kind: 'starting' }
  | { readonly kind: 'request'; readonly prepared: PreparedRequest }
  | { readonly kind: 'error'; readonly code: string };

const BUTTON = 'min-h-11 self-start rounded border border-gray-400 px-4';
const PRIMARY = 'min-h-11 self-start rounded bg-blue-700 px-4 text-white';

export type ConnectMethod = 'device_flow' | 'token';

/** Failures of a stored token with a text of their own. */
const TOKEN_FAILURES: ReadonlySet<string> = new Set([
  'token_invalid',
  'token_refused',
  'scope_missing',
  'sso_required',
  'upstream_unreachable',
]);

/** The text of a failure of the flow: the token's own texts first (module comment). */
function useFailure(method: ConnectMethod): (code: string) => string {
  const { t } = useI18n();
  const reason = useReason();
  return (code) => (method === 'token' && TOKEN_FAILURES.has(code) ? t(`github.token.failure.${code}`) : reason(code));
}

/** `m:ss` of a remaining time. */
export function remaining(ms: number): string {
  const seconds = Math.max(0, Math.ceil(ms / 1000));
  return `${Math.floor(seconds / 60)}:${String(seconds % 60).padStart(2, '0')}`;
}

function DeviceCode({ progress }: { readonly progress: Extract<ConnectProgress, { step: 'device_code' }> }) {
  const { t } = useI18n();
  const now = useNow(true, 1000);
  const [copied, setCopied] = useState(false);
  const left = progress.expiresAt - now;
  const copyLabel = copied ? t('github.code.copied') : t('github.code.copy');
  const copy = async () => {
    try {
      await navigator.clipboard.writeText(progress.userCode);
      setCopied(true);
    } catch {
      setCopied(false);
    }
  };
  return (
    <div className="flex flex-col gap-3" data-testid="github.code">
      <p>{t('github.code.intro')}</p>
      <p className="font-mono text-3xl font-semibold tracking-widest" data-testid="github.code.value">
        {progress.userCode}
      </p>
      <div className="flex flex-wrap gap-2">
        <button type="button" className={BUTTON} onClick={() => void copy()} data-testid="github.code.copy">
          {copyLabel}
        </button>
        <a
          className={`${PRIMARY} flex items-center`}
          href={progress.verificationUri}
          target="_blank"
          rel="noopener noreferrer"
          data-testid="github.code.open"
        >
          {t('github.code.open')}
        </a>
      </div>
      <p data-testid="github.code.expires" data-expired={left <= 0}>
        {left > 0 ? t('github.code.expires', { time: remaining(left) }) : t('github.code.expired')}
      </p>
      {/* announced once per started minute and at expiry, not every second (06-web.md section 12.3) */}
      <LiveRegion
        text={
          left > 0 ? t('github.code.expires.announce', { minutes: Math.ceil(left / 60_000) }) : t('github.code.expired')
        }
        testId="github.code.announce"
      />
      <p className="text-sm text-gray-600">{t('github.code.hint')}</p>
    </div>
  );
}

function Request({
  prepared,
  method,
  onConnected,
  onRetry,
}: {
  readonly prepared: PreparedRequest;
  readonly method: ConnectMethod;
  readonly onConnected: () => void;
  readonly onRetry: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const failure = useFailure(method);
  const [view, setView] = useState<RequestView | null>(null);
  const [readError, setReadError] = useState<string | null>(null);
  useEffect(
    () =>
      watchRequest(app.tasks, prepared, {
        onView: (next) => {
          setReadError(null);
          setView(next);
        },
        onError: setReadError,
      }),
    [app, prepared],
  );
  const progress: ConnectProgress = view === null ? { step: 'waiting' } : connectProgress(view);
  const done = progress.step === 'done';
  useEffect(() => {
    if (done) onConnected();
  }, [done, onConnected]);
  if (progress.step === 'waiting')
    return (
      <div className="flex flex-col gap-1">
        <p aria-live="polite" data-testid="github.waiting">
          {method === 'token' ? t('github.token.checking') : t('github.waiting')}
        </p>
        {readError !== null && (
          <p className="text-sm text-red-800" data-testid="github.read-failed" data-code={readError}>
            {t('github.read-failed', { reason: reason(readError) })}
          </p>
        )}
      </div>
    );
  if (progress.step === 'device_code') return <DeviceCode progress={progress} />;
  if (progress.step === 'done')
    return (
      <p role="status" data-testid="github.done">
        {t('github.done', { login: progress.login })}
      </p>
    );
  return (
    <div className="flex flex-col gap-2">
      <p role="alert" data-testid="github.failed" data-code={progress.code}>
        {t('github.failed', { reason: failure(progress.code) })}
      </p>
      <button type="button" className={BUTTON} onClick={onRetry} data-testid="github.retry">
        {t('github.retry')}
      </button>
    </div>
  );
}

/** The token field (module comment): uncontrolled, so its value lives only in the input element. */
function TokenField({ field }: { readonly field: RefObject<HTMLInputElement> }) {
  const { t } = useI18n();
  const id = useId();
  return (
    <div className="flex flex-col gap-1">
      <label htmlFor={id}>{t('github.token.label')}</label>
      <input
        id={id}
        ref={field}
        type="password"
        name="github-token-input"
        autoComplete="off"
        autoCapitalize="off"
        autoCorrect="off"
        spellCheck={false}
        data-1p-ignore="true"
        data-lpignore="true"
        className="min-h-11 rounded border border-gray-400 px-3 font-mono"
        data-testid="github.token.input"
      />
    </div>
  );
}

export function ConnectFlow({
  method = 'device_flow',
  onConnected,
  onClose,
}: {
  readonly method?: ConnectMethod;
  readonly onConnected: () => void;
  readonly onClose: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const deviceName = useProfileDeviceNames();
  const selectId = useId();
  const field = useRef<HTMLInputElement>(null);
  const [empty, setEmpty] = useState(false);
  const [phase, setPhase] = useState<Phase>({ kind: 'devices' });
  useEffect(() => {
    if (phase.kind !== 'devices') return;
    // while the devices load the flow offers only "Close", which unmounts it: a late answer changes nothing
    connectDevices(app.tasks).then(
      (found) =>
        setPhase(
          found.preselected === null
            ? { kind: 'none' }
            : { kind: 'choose', devices: found.devices, selected: found.preselected },
        ),
      (error: unknown) => setPhase({ kind: 'error', code: requestErrorCode(error) }),
    );
  }, [app, phase]);
  const start = async (device: string) => {
    let token: string | undefined;
    if (method === 'token') {
      const input = field.current!;
      const checked = tokenInput(input.value);
      input.value = '';
      setEmpty(checked === null);
      if (checked === null) return;
      token = checked;
    }
    setPhase({ kind: 'starting' });
    try {
      setPhase({ kind: 'request', prepared: await startConnect(app.tasks, device, token) });
    } catch (error) {
      setPhase({ kind: 'error', code: requestErrorCode(error) });
    }
  };
  const retry = () => setPhase({ kind: 'devices' });
  return (
    <section className="flex flex-col gap-3 rounded border border-gray-300 p-4" data-testid="github.flow">
      <h3 className="font-semibold">{method === 'token' ? t('github.token.title') : t('github.flow.title')}</h3>
      {method === 'token' && (
        <>
          <p className="text-sm">{t('github.token.intro')}</p>
          <p className="text-sm text-gray-700" data-testid="github.token.recommend">
            {t('github.token.recommend')}
          </p>
        </>
      )}
      {(phase.kind === 'devices' || phase.kind === 'starting') && (
        <p aria-live="polite" data-testid="github.devices.loading">
          {t('github.devices.loading')}
        </p>
      )}
      {phase.kind === 'none' && (
        <>
          <p data-testid="github.devices.none">{t('github.devices.none')}</p>
          <button type="button" className={BUTTON} onClick={retry} data-testid="github.retry">
            {t('github.retry')}
          </button>
        </>
      )}
      {phase.kind === 'error' && (
        <>
          <p role="alert" data-testid="github.failed" data-code={phase.code}>
            {t('github.devices.failed', { reason: reason(phase.code) })}
          </p>
          <button type="button" className={BUTTON} onClick={retry} data-testid="github.retry">
            {t('github.retry')}
          </button>
        </>
      )}
      {phase.kind === 'choose' && (
        <>
          <label htmlFor={selectId}>{t('github.devices.label')}</label>
          <select
            id={selectId}
            className="min-h-11 rounded border border-gray-400 px-3"
            value={phase.selected}
            onChange={(event) => setPhase({ ...phase, selected: event.target.value })}
            data-testid="github.devices.select"
          >
            {phase.devices.map((device) => {
              const label = t('github.devices.option', { device: deviceName(device.device) });
              const named = device.defaultWg ? t('github.devices.default', { label }) : label;
              return (
                <option key={device.device} value={device.device} disabled={!device.online}>
                  {device.online ? named : t('github.devices.offline', { label: named })}
                </option>
              );
            })}
          </select>
          {method === 'token' && <TokenField field={field} />}
          {empty && (
            <p role="alert" className="text-sm text-red-800" data-testid="github.token.empty">
              {t('github.token.empty')}
            </p>
          )}
          <button
            type="button"
            className={PRIMARY}
            onClick={() => void start(phase.selected)}
            data-testid="github.start"
          >
            {method === 'token' ? t('github.token.submit') : t('github.start')}
          </button>
        </>
      )}
      {phase.kind === 'request' && (
        <Request prepared={phase.prepared} method={method} onConnected={onConnected} onRetry={retry} />
      )}
      <button type="button" className={BUTTON} onClick={onClose} data-testid="github.flow.close">
        {t('github.close')}
      </button>
    </section>
  );
}
