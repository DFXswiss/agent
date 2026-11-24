// The patch of a file change (06-web.md section 5.2, HOT-05): a button "Show changes" (`aria-expanded`); the patch is
// rendered only while open – a closed patch is not in the DOM – and at most `CLIP_CHARS` characters at a time, the
// rest behind "Show more".
import { useId, useState } from 'react';
import { useI18n } from '../../i18n';
import { Clipped, MORE } from '../../lists/Clipped';

export function FilePatch({ patch }: { readonly patch: string }) {
  const { t } = useI18n();
  const id = useId();
  const [open, setOpen] = useState(false);
  return (
    <>
      <button
        type="button"
        className={MORE}
        aria-expanded={open}
        {...(open && { 'aria-controls': id })}
        onClick={() => setOpen(!open)}
        data-testid="chat.file.toggle"
      >
        {t(open ? 'chat-list.patch.hide' : 'task.chat.file.patch')}
      </button>
      {open && (
        <div id={id} className="flex flex-col gap-1">
          <Clipped
            text={patch}
            className="max-h-96 overflow-auto rounded bg-gray-50 p-2 font-mono text-xs"
            testId="chat.file.patch"
            controls="chat"
          />
        </div>
      )}
    </>
  );
}
