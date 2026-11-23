// The editor of an entry (06-web.md section 6.1): a labelled Markdown text area; for a new entry the optional
// checkbox "Notify participants" (`notify:true`, 03-api.md section 4.9). Save is possible with 1–65 536 characters;
// a failure shows the catalog message and keeps the text (a retry re-sends the same sealed event).
import { useId, useState } from 'react';
import { sendErrorCode } from '../../core/tasks/send';
import { validText } from '../../core/whiteboards/project';
import { useI18n } from '../i18n';

export interface EditorProps {
  readonly initial: string;
  /** Show the notify checkbox (new entries only). */
  readonly withNotify: boolean;
  readonly onSave: (text: string, notify: boolean) => Promise<void>;
  readonly onCancel: () => void;
}

export function Editor({ initial, withNotify, onSave, onCancel }: EditorProps) {
  const { t, error } = useI18n();
  const textId = useId();
  const [text, setText] = useState(initial);
  const [notify, setNotify] = useState(false);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const save = async () => {
    setBusy(true);
    setCode(null);
    try {
      await onSave(text, notify);
    } catch (failure) {
      setCode(sendErrorCode(failure));
      setBusy(false);
    }
  };
  return (
    <form
      className="flex flex-col gap-2"
      onSubmit={(event) => {
        event.preventDefault();
        void save();
      }}
      data-testid="whiteboard.editor"
    >
      <label htmlFor={textId} className="text-sm font-medium">
        {t('whiteboard.editor.label')}
      </label>
      <textarea
        id={textId}
        className="min-h-32 rounded border border-gray-300 p-2 font-mono text-sm"
        value={text}
        onChange={(event) => setText(event.target.value)}
        data-testid="whiteboard.editor.text"
      />
      {withNotify && (
        <label className="flex min-h-11 items-center gap-2 text-sm">
          <input
            type="checkbox"
            className="h-5 w-5"
            checked={notify}
            onChange={(event) => setNotify(event.target.checked)}
            data-testid="whiteboard.editor.notify"
          />
          {t('whiteboard.editor.notify')}
        </label>
      )}
      {code !== null && (
        <p role="alert" className="text-sm text-red-800" data-testid="whiteboard.editor.error" data-code={code}>
          {t('whiteboard.editor.failed', { reason: error(code).title })}
        </p>
      )}
      <div className="flex gap-2">
        <button
          type="submit"
          className="min-h-11 rounded bg-blue-700 px-4 text-white disabled:opacity-50"
          disabled={busy || !validText(text)}
          data-testid="whiteboard.editor.save"
        >
          {busy ? t('whiteboard.editor.saving') : t('whiteboard.editor.save')}
        </button>
        <button
          type="button"
          className="min-h-11 rounded border border-gray-300 px-4"
          onClick={onCancel}
          data-testid="whiteboard.editor.cancel"
        >
          {t('whiteboard.cancel')}
        </button>
      </div>
    </form>
  );
}
