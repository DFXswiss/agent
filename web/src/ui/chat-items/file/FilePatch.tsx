// The patch of a file change (06-web.md section 5.2, HOT-05): a button "Show changes" (`aria-expanded`); the patch is
// rendered only while open – a closed patch is not in the DOM – and at most `PATCH_CHARS` characters at a time, the
// rest behind "Show more".
import { useId, useState } from 'react';
import { formatNumber } from '../../../core/i18n';
import { useI18n } from '../../i18n';

export const PATCH_CHARS = 20_000;

export function FilePatch({ patch }: { readonly patch: string }) {
  const { t, locale } = useI18n();
  const id = useId();
  const [open, setOpen] = useState(false);
  const [limit, setLimit] = useState(PATCH_CHARS);
  return (
    <>
      <button
        type="button"
        className="min-h-11 self-start py-2 text-left text-blue-800 underline"
        aria-expanded={open}
        {...(open && { 'aria-controls': id })}
        onClick={() => setOpen(!open)}
        data-testid="chat.file.toggle"
      >
        {t(open ? 'chat-list.patch.hide' : 'task.chat.file.patch')}
      </button>
      {open && (
        <div id={id} className="flex flex-col gap-1">
          <pre
            className="max-h-96 overflow-auto rounded bg-gray-50 p-2 font-mono text-xs"
            data-testid="chat.file.patch"
          >
            {patch.slice(0, limit)}
          </pre>
          {patch.length > limit && (
            <button
              type="button"
              className="min-h-11 self-start py-2 text-left text-blue-800 underline"
              onClick={() => setLimit(limit + PATCH_CHARS)}
              data-testid="chat.file.patch.more"
            >
              {t('chat-list.patch.more', { count: formatNumber(patch.length - limit, locale) })}
            </button>
          )}
        </div>
      )}
    </>
  );
}
