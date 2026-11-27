// The composer of the home screen and the task list (06-web.md sections 4.1, 4.3, 5.1, 12.5): suggestion chips that
// fill the field (never send), a multi-line prompt field, send with the button or `Ctrl/⌘+Enter`. Every send creates a
// task: device check (automatic assignment where allowed), sealed `task.meta` + `prompt`, `POST /v1/tasks`, then the
// task page at once. While the send runs, the countdown starts (`PreparingPanel`). A failed send keeps the prepared
// request: sending the unchanged prompt to the same workgroup again re-uses its IDs (idempotent). Offline, the button
// says "Offline" and nothing is queued. GitHub (KOR-25 part three, ADR-066): when the GitHub chip was used or the
// text obviously refers to GitHub (`core/github/mention.ts`) and the GitHub connection is not there, the composer
// offers connecting right here (device flow or an existing token, `GithubConnect`); the task is sent either way. The
// connection state is loaded once when first needed; a typed reference shows the hint only once the state is known (a
// connected user sees nothing flash). The offer sits below the form: its token field belongs to no form, and Enter
// there never sends the task. The device choice (ADR-075 section 6) goes into the request as `placement`;
// "Automatic" while the workgroup has a default is applied to the created task afterwards (`PUT
// /v1/tasks/{task}/placement`, best effort), since a request without `placement` takes the default.
// When the user may choose a GitHub identity (ADR-078 section 3: own accounts, company accounts of the
// organization), a choice "GitHub account" goes with the task (`github_identity`; default automatic).
// Files and images (ADR-102, `Attachments.tsx`): attached by the button, by dropping them onto the composer or by
// pasting into the prompt field; uploaded and committed before the prompt is sealed, named in its `attachments`.
import { type KeyboardEvent, useEffect, useId, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { loadGithub } from '../../core/github/connection';
import { mentionsGithub } from '../../core/github/mention';
import { applyAutomatic, samePlacement } from '../../core/tasks/placement';
import { type PreparedTask, prepareTask, sendable, sendErrorCode, sendTask } from '../../core/tasks/send';
import { PlacementSelect, useDeviceChoice } from './DeviceChoice';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';
import { PreparingPanel } from '../countdown/PreparingPanel';
import { useAboveKeyboard } from '../responsive/keyboard';
import { GithubConnect } from '../settings/sections/github/Connect';
import { useDirectory } from '../screens/tasks/hooks';
import { AUTOMATIC, IdentityChoice } from './IdentityChoice';
import { AttachButton, attachHandlers, AttachmentList, useAttachments } from './Attachments';
import type { AttachmentRef } from '../../core/attachments/transfer';

/** Whether two lists of attachment references name the same objects in the same order. */
export function sameObjects(a: readonly AttachmentRef[], b: readonly AttachmentRef[]): boolean {
  return a.map((ref) => ref.object).join() === b.map((ref) => ref.object).join();
}

/** Failures after which the same request (same IDs) is sent again: the hub may not have seen it or may succeed later. */
export const RETRYABLE: ReadonlySet<string> = new Set(['unavailable', 'rate_limited', 'seq_conflict']);

/** The suggestion chips (06-web.md section 4.1), in this order. */
export const CHIPS = ['research', 'github', 'explain', 'plan'] as const;

export interface ComposerProps {
  readonly org: string;
  readonly wg: string;
  /** Show the suggestion chips (home screen). */
  readonly suggestions?: boolean;
}

type Status =
  | { readonly kind: 'idle' }
  | { readonly kind: 'sending'; readonly startedAt: number; readonly assigning: boolean }
  | { readonly kind: 'error'; readonly code: string };

export function Composer({ org, wg, suggestions = false }: ComposerProps) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const navigate = useNavigate();
  const online = useStore((state) => state.connection.online, app.store);
  const githubStatus = useStore((state) => state.github.status, app.store);
  const [text, setText] = useState('');
  const [status, setStatus] = useState<Status>({ kind: 'idle' });
  const [github, setGithub] = useState(false);
  const [identity, setIdentity] = useState(AUTOMATIC);
  const directory = useDirectory(org, false);
  const prepared = useRef<PreparedTask | null>(null);
  const { devices, choice, setChoice } = useDeviceChoice(wg);
  const hasDefault = devices !== null && devices.placement !== null;
  const field = useRef<HTMLTextAreaElement>(null);
  // stays above the on-screen keyboard while typing (06-web.md section 12.1)
  const form = useRef<HTMLFormElement>(null);
  useAboveKeyboard(form);
  const label = useId();
  const sending = status.kind === 'sending';
  const disabled = sending || !online || !sendable(text);
  const attachments = useAttachments();
  const handlers = attachHandlers(attachments, sending);
  const mention = mentionsGithub(text);
  const offerGithub = githubStatus !== 'connected' && (github || (mention && githubStatus !== 'unknown'));
  const githubLoad = useRef(false);
  useEffect(() => {
    // loaded once when first needed (module comment)
    if (!(github || mention) || githubStatus !== 'unknown' || githubLoad.current) return;
    githubLoad.current = true;
    loadGithub(app.tasks).catch(() => undefined);
  }, [app, github, mention, githubStatus]);

  const send = async () => {
    if (disabled) return;
    const startedAt = Date.now();
    setStatus({ kind: 'sending', startedAt, assigning: false });
    try {
      // the files first: the prompt names only objects the hub holds complete (ADR-102)
      const refs = await attachments.upload(app.tasks, org, wg);
      const known = prepared.current;
      const task =
        known !== null &&
        known.org === org &&
        known.wg === wg &&
        known.prompt === text &&
        sameObjects(known.attachments, refs) &&
        samePlacement(known.placement, choice) &&
        (known.body.github_identity ?? AUTOMATIC) === identity
          ? known
          : await prepareTask(app.tasks, {
              org,
              wg,
              prompt: text,
              attachments: refs,
              placement: choice,
              ...(identity !== AUTOMATIC && { githubIdentity: identity }),
            });
      prepared.current = task;
      const sent = await sendTask(app.tasks, task, {
        onAssigning: () => setStatus({ kind: 'sending', startedAt, assigning: true }),
      });
      prepared.current = null;
      attachments.clear();
      await applyAutomatic(app.tasks, sent.task.task_id, choice, hasDefault);
      navigate(`/o/${encodeURIComponent(org)}/t/${encodeURIComponent(sent.task.task_id)}`);
    } catch (failure) {
      const code = sendErrorCode(failure);
      // only a retry that can succeed re-uses the request; anything else is prepared anew (e.g. after a rotation)
      if (!RETRYABLE.has(code)) prepared.current = null;
      setStatus({ kind: 'error', code });
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void send();
    }
  };

  const fill = (chip: (typeof CHIPS)[number]) => {
    setText(t(`home.chip.${chip}.text`));
    // the chip offers "Connect GitHub" only while no connection is known (06-web.md section 4.1, WEB-09)
    setGithub(chip === 'github');
    field.current?.focus();
  };

  return (
    <div className="flex flex-col gap-3">
      <form
        ref={form}
        className="flex flex-col gap-3"
        onSubmit={(event) => {
          event.preventDefault();
          void send();
        }}
        onDrop={handlers.onDrop}
        onDragOver={handlers.onDragOver}
        data-testid="composer"
      >
        {suggestions && (
          <ul className="flex flex-wrap gap-2" aria-label={t('home.chips.label')} data-testid="composer.chips">
            {CHIPS.map((chip) => (
              <li key={chip}>
                <button
                  type="button"
                  className="min-h-11 rounded-full border border-gray-300 px-3 text-sm"
                  onClick={() => fill(chip)}
                  data-testid={`composer.chip.${chip}`}
                >
                  {t(`home.chip.${chip}`)}
                </button>
              </li>
            ))}
          </ul>
        )}
        <label htmlFor={label} className="sr-only">
          {t('home.prompt.label')}
        </label>
        <textarea
          id={label}
          ref={field}
          className="min-h-28 w-full rounded border border-gray-300 p-2"
          placeholder={t('home.prompt.placeholder')}
          value={text}
          onChange={(event) => setText(event.target.value)}
          onKeyDown={onKeyDown}
          onPaste={handlers.onPaste}
          disabled={sending}
          data-testid="composer.prompt"
        />
        <AttachmentList attachments={attachments} disabled={sending} testId="composer.attachments" />
        {devices !== null && devices.devices.length > 0 && (
          <PlacementSelect
            org={org}
            devices={devices}
            value={choice}
            onChange={setChoice}
            testId="home"
            disabled={sending}
          />
        )}
        <IdentityChoice
          org={org}
          orgName={directory.orgName}
          value={identity}
          onChange={setIdentity}
          disabled={sending}
          testId="composer.identity"
        />
        <div className="flex items-center gap-3">
          <button
            type="submit"
            className="min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50"
            disabled={disabled}
            data-testid="composer.send"
          >
            {online ? t('home.send') : t('home.offline')}
          </button>
          <AttachButton attachments={attachments} disabled={sending} testId="composer.attach" />
          <span className="hidden text-xs text-gray-600 md:inline">{t('home.send.shortcut')}</span>
        </div>
        {status.kind === 'error' && (
          <div
            role="alert"
            className="rounded border border-red-200 bg-red-50 p-3 text-red-900"
            data-testid="composer.error"
            data-code={status.code}
          >
            <p className="font-semibold">{error(status.code).title}</p>
            <p>{error(status.code).body}</p>
          </div>
        )}
        {status.kind === 'sending' && <PreparingPanel startedAt={status.startedAt} assigning={status.assigning} />}
      </form>
      {offerGithub && (
        <div className="flex flex-col gap-2 rounded border border-gray-300 p-3" data-testid="composer.github-hint">
          <p className="text-sm">{t('github.composer.hint')}</p>
          <GithubConnect />
        </div>
      )}
    </div>
  );
}
