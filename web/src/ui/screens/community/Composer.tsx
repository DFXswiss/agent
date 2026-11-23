// The composer of the community (12-community.md section 6): a textarea, Enter sends, Shift+Enter breaks the line, a
// send button (phones), a character counter near the limit. Before the first own post it shows the display name
// (from `Community.defaultName`, editable, remembered per user in this browser) with the notice that the name is
// visible to all users and that community messages are not confidential; afterwards "You write as …" with a button
// to change the name. A refused post keeps the text; `429` shows the wait time, everything else the error catalog.
import { type KeyboardEvent, useId, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import { type Community, codeOf } from '../../../core/community/engine';
import { cleanName, cleanText } from '../../../core/crypto/community';
import { HubError } from '../../../core/net/signedFetch';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAboveKeyboard } from '../../responsive/keyboard';
import { useAppEnv } from '../../shell/context';

/** The counter appears when at most this share of the limit is left. */
export const NEAR_LIMIT = 0.1;

type Status =
  | { readonly kind: 'idle' }
  | { readonly kind: 'sending' }
  | { readonly kind: 'error'; readonly code: string; readonly retryAfter: number | null };

const length = (text: string): number => [...text].length;

export function Composer({ engine, firstPost }: { readonly engine: Community; readonly firstPost: boolean }) {
  const { app } = useAppEnv();
  const { t, error } = useI18n();
  const online = useStore((state) => state.connection.online, app.store);
  const textChars = useStore((state) => state.community.textChars, app.store);
  const nameChars = useStore((state) => state.community.nameChars, app.store);
  // re-read the offered name when the loaded messages change (the newest own message names it)
  useStore((state) => state.community.messages, app.store);
  const [text, setText] = useState('');
  const [typedName, setTypedName] = useState<string | null>(null);
  const [editing, setEditing] = useState(false);
  const [status, setStatus] = useState<Status>({ kind: 'idle' });
  const form = useRef<HTMLFormElement>(null);
  useAboveKeyboard(form);
  const fieldId = useId();
  const nameId = useId();
  const name = typedName ?? engine.defaultName();
  const cleanedName = cleanName(name);
  const cleanedText = cleanText(text);
  const nameValid = length(cleanedName) >= 1 && length(cleanedName) <= nameChars;
  const left = textChars - length(cleanedText);
  const sending = status.kind === 'sending';
  const disabled = sending || !online || !nameValid || cleanedText === '' || left < 0;
  const showName = firstPost || editing || !nameValid;

  const send = async () => {
    if (disabled) return;
    setStatus({ kind: 'sending' });
    const sent = text;
    try {
      await engine.post(cleanedName, cleanedText);
      // what was typed while the post ran stays
      setText((current) => (current === sent ? '' : current));
      setTypedName(null);
      setEditing(false);
      setStatus({ kind: 'idle' });
    } catch (failure) {
      const retryAfter = failure instanceof HubError ? failure.retryAfter : null;
      setStatus({ kind: 'error', code: codeOf(failure), retryAfter });
    }
  };

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key !== 'Enter' || event.shiftKey || event.nativeEvent.isComposing) return;
    event.preventDefault();
    void send();
  };

  return (
    <form
      ref={form}
      className="flex flex-col gap-2 border-t border-gray-200 pt-3"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
      data-testid="community.composer"
    >
      {showName ? (
        <div
          className="flex flex-col gap-2 rounded border border-amber-300 bg-amber-50 p-3"
          data-testid="community.notice"
        >
          <p data-testid="community.notice.text">{t('community.name.notice')}</p>
          <Link className="self-start text-sm underline" to="/security" data-testid="community.notice.security">
            {t('community.name.more')}
          </Link>
          <label htmlFor={nameId} className="font-medium">
            {t('community.name.label')}
          </label>
          <input
            id={nameId}
            className="min-h-11 rounded border border-gray-400 bg-white px-3"
            value={name}
            maxLength={nameChars * 2}
            autoComplete="name"
            onChange={(event) => setTypedName(event.target.value)}
            aria-invalid={!nameValid}
            data-testid="community.name"
          />
          {!nameValid && (
            <p className="text-sm text-red-800" data-testid="community.name.missing">
              {t('community.name.missing')}
            </p>
          )}
        </div>
      ) : (
        <p className="flex flex-wrap items-center gap-x-2 text-sm text-gray-700" data-testid="community.name.as">
          <span>{t('community.name.as', { name: cleanedName })}</span>
          <button
            type="button"
            className="min-h-11 underline"
            onClick={() => setEditing(true)}
            data-testid="community.name.change"
          >
            {t('community.name.change')}
          </button>
        </p>
      )}
      <label htmlFor={fieldId} className="sr-only">
        {t('community.composer.label')}
      </label>
      <textarea
        id={fieldId}
        className="min-h-20 w-full rounded border border-gray-300 p-2"
        placeholder={t('community.composer.placeholder')}
        value={text}
        rows={3}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        aria-describedby={`${fieldId}-hint`}
        data-testid="community.composer.text"
      />
      <div className="flex flex-wrap items-center gap-3">
        <button
          type="submit"
          className="min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50"
          disabled={disabled}
          data-testid="community.composer.send"
        >
          {online ? t('community.composer.send') : t('community.composer.offline')}
        </button>
        <span id={`${fieldId}-hint`} className="text-xs text-gray-600">
          {t('community.composer.hint')}
        </span>
        {left <= textChars * NEAR_LIMIT && (
          <span
            className={`ml-auto text-sm ${left < 0 ? 'font-semibold text-red-800' : 'text-gray-700'}`}
            aria-live="polite"
            data-testid="community.composer.counter"
          >
            {left < 0 ? t('community.composer.over', { count: -left }) : t('community.composer.left', { count: left })}
          </span>
        )}
      </div>
      {status.kind === 'error' && (
        <div
          role="alert"
          className="rounded border border-red-200 bg-red-50 p-3 text-red-900"
          data-testid="community.composer.error"
          data-code={status.code}
        >
          {status.code === 'rate_limited' ? (
            <p>
              {status.retryAfter === null
                ? t('community.rate.short')
                : t('community.rate', { seconds: Math.max(1, status.retryAfter) })}
            </p>
          ) : (
            <>
              <p className="font-semibold">{error(status.code).title}</p>
              <p>{error(status.code).body}</p>
            </>
          )}
        </div>
      )}
    </form>
  );
}
