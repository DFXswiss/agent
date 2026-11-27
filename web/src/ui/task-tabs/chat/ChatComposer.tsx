// Input of the chat (06-web.md section 5.2), only for users with `task:write`: a follow-up prompt (`Ctrl/⌘+Enter` or
// the button; it stays enabled while the main session works – the session queues the prompt), **Stop** (`interrupt`)
// while the main session is `working`, and in the session menu **End session** (`stop`) while it is alive. A failed
// send keeps the sealed prompt for an identical retry (`core/chat/actions.ts`). Offline, the button says so.
// `useComposer` holds text, sending, controls and the failure; the input of the task tab "Claude Code"
// (`task-tabs/terminal/ClaudeInput.tsx`, KONZEPT E-43) uses it as well, so both send the same signed prompt.
import { type KeyboardEvent, useId, useRef, useState } from 'react';
import { controlSession, type ControlAction, sendPrompt } from '../../../core/chat/actions';
import type { SessionEntry, TaskRow } from '../../../core/store/slices/tasks';
import { sendable, sendErrorCode } from '../../../core/tasks/send';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAboveKeyboard } from '../../responsive/keyboard';
import { useAppEnv } from '../../shell/context';

/** Session states in which a session can still be stopped. */
const ENDED = new Set(['stopping', 'stopped', 'failed']);

export interface Composer {
  readonly text: string;
  readonly setText: (text: string) => void;
  readonly online: boolean;
  /** Sending is not possible now (sending, offline or nothing to send). */
  readonly disabled: boolean;
  readonly failure: { readonly key: string; readonly code: string } | null;
  /** The main session works (Stop / Esc interrupt it). */
  readonly working: boolean;
  /** The main session is alive (End session stops it). */
  readonly alive: boolean;
  readonly send: () => Promise<void>;
  readonly control: (action: ControlAction) => Promise<void>;
}

/** The state and actions of a prompt input of `task` (its main session `main`). */
export function useComposer(task: TaskRow, main: SessionEntry | null): Composer {
  const { app } = useAppEnv();
  const online = useStore((state) => state.connection.online, app.store);
  const [text, setText] = useState('');
  const [sending, setSending] = useState(false);
  const [failure, setFailure] = useState<Composer['failure']>(null);
  const disabled = sending || !online || !sendable(text);

  const send = async () => {
    if (disabled) return;
    setSending(true);
    setFailure(null);
    try {
      await sendPrompt(app.tasks, task, text);
      setText('');
    } catch (thrown) {
      setFailure({ key: 'task.chat.send.failed', code: sendErrorCode(thrown) });
    }
    setSending(false);
  };

  const control = async (action: ControlAction) => {
    setFailure(null);
    try {
      await controlSession(app.tasks, main!.row.session_id, action);
    } catch (thrown) {
      setFailure({ key: 'task.chat.control.failed', code: sendErrorCode(thrown) });
    }
  };

  return {
    text,
    setText,
    online,
    disabled,
    failure,
    working: main?.row.state === 'working',
    alive: main !== null && !ENDED.has(main.row.state),
    send,
    control,
  };
}

export function ChatComposer({ task, main }: { readonly task: TaskRow; readonly main: SessionEntry | null }) {
  const { t, error } = useI18n();
  const composer = useComposer(task, main);
  const { text, setText, online, disabled, failure, working, alive, send, control } = composer;
  const label = useId();

  const onKeyDown = (event: KeyboardEvent<HTMLTextAreaElement>) => {
    if (event.key === 'Enter' && (event.ctrlKey || event.metaKey)) {
      event.preventDefault();
      void send();
    }
  };

  // stays above the on-screen keyboard while typing (06-web.md section 12.1)
  const form = useRef<HTMLFormElement>(null);
  useAboveKeyboard(form);
  return (
    <form
      ref={form}
      className="flex flex-col gap-2 border-t border-gray-200 pt-3"
      onSubmit={(event) => {
        event.preventDefault();
        void send();
      }}
      data-testid="chat.composer"
    >
      <label htmlFor={label} className="sr-only">
        {t('task.chat.prompt.label')}
      </label>
      <textarea
        id={label}
        className="min-h-20 w-full rounded border border-gray-300 p-2"
        placeholder={t('task.chat.prompt.placeholder')}
        value={text}
        onChange={(event) => setText(event.target.value)}
        onKeyDown={onKeyDown}
        data-testid="chat.composer.prompt"
      />
      <div className="flex flex-wrap items-center gap-2">
        <button
          type="submit"
          className="min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50"
          disabled={disabled}
          data-testid="chat.composer.send"
        >
          {online ? t('task.chat.send') : t('task.chat.offline')}
        </button>
        {working && (
          <button
            type="button"
            className="min-h-11 rounded border border-red-700 px-4 text-red-800"
            onClick={() => void control('interrupt')}
            data-testid="chat.control.stop"
          >
            {t('task.chat.stop')}
          </button>
        )}
        {alive && (
          <details className="relative" data-testid="chat.control.menu">
            <summary className="flex min-h-11 cursor-pointer items-center rounded border border-gray-300 px-3">
              {t('task.chat.more')}
            </summary>
            <div className="absolute bottom-12 left-0 z-10 rounded border border-gray-200 bg-white p-1 shadow">
              <button
                type="button"
                className="min-h-11 whitespace-nowrap px-3 text-left text-red-800"
                onClick={() => void control('stop')}
                data-testid="chat.control.end"
              >
                {t('task.chat.end')}
              </button>
            </div>
          </details>
        )}
        <span className="hidden text-xs text-gray-600 md:inline">{t('task.chat.send.shortcut')}</span>
      </div>
      {failure !== null && (
        <p className="text-sm text-red-800" role="alert" data-testid="chat.composer.error" data-code={failure.code}>
          {t(failure.key, { reason: error(failure.code).title })}
        </p>
      )}
    </form>
  );
}
