// The menu of a community message (12-community.md section 6): the author's own "Delete", a moderator's "Remove" and
// "Mute author" (not for moderators, who cannot be muted), each confirmed in the sheet before it runs. Failures are
// shown through the error catalog; a mute ends with a notice on the screen.
import { useState } from 'react';
import { type Community, codeOf } from '../../../core/community/engine';
import type { CommunityMessage } from '../../../core/store/slices/community';
import { useI18n } from '../../i18n';
import { Sheet } from '../../responsive/Sheet';

type Action = 'delete' | 'remove' | 'mute';

/** The actions the viewer has on a message. */
export function actionsOf(message: CommunityMessage, own: boolean, moderator: boolean): Action[] {
  if (message.removed) return [];
  if (own) return ['delete'];
  if (!moderator) return [];
  return message.moderator ? ['remove'] : ['remove', 'mute'];
}

export function MessageActions({
  message,
  name,
  own,
  moderator,
  engine,
  onNotice,
}: {
  readonly message: CommunityMessage;
  readonly name: string;
  readonly own: boolean;
  readonly moderator: boolean;
  readonly engine: Community;
  readonly onNotice: (text: string) => void;
}) {
  const { t, error } = useI18n();
  const [open, setOpen] = useState(false);
  const [chosen, setChosen] = useState<Action | null>(null);
  const [busy, setBusy] = useState(false);
  const [code, setCode] = useState<string | null>(null);
  const actions = actionsOf(message, own, moderator);
  if (actions.length === 0) return null;
  const close = () => {
    setOpen(false);
    setChosen(null);
    setCode(null);
  };
  const run = async (action: Action) => {
    setBusy(true);
    setCode(null);
    try {
      if (action === 'mute') {
        await engine.setMuted(message.author, true);
        onNotice(t('community.mute.done', { name }));
      } else {
        await engine.remove(message.id);
      }
      close();
    } catch (failure) {
      setCode(codeOf(failure));
    } finally {
      setBusy(false);
    }
  };
  return (
    <>
      <button
        type="button"
        className="ml-auto flex min-h-11 min-w-11 items-center justify-center rounded text-lg leading-none text-gray-700 hover:bg-gray-200"
        aria-haspopup="dialog"
        aria-label={t('community.menu', { name })}
        onClick={() => setOpen(true)}
        data-testid="community.message.menu"
      >
        ⋯
      </button>
      {open && (
        <Sheet title={t('community.menu.title')} testId="community.actions" busy={busy} onClose={close}>
          {chosen === null ? (
            <div className="flex flex-col gap-2">
              {actions.map((action) => (
                <button
                  key={action}
                  type="button"
                  className="min-h-11 rounded border border-gray-300 px-4 text-left"
                  onClick={() => setChosen(action)}
                  data-testid={`community.actions.${action}`}
                >
                  {t(`community.${action}`)}
                </button>
              ))}
            </div>
          ) : (
            <div className="flex flex-col gap-3">
              <p data-testid="community.actions.question">{t(`community.${chosen}.confirm`, { name })}</p>
              <button
                type="button"
                className="min-h-11 rounded bg-red-700 px-4 text-white disabled:opacity-50"
                disabled={busy}
                onClick={() => void run(chosen)}
                data-testid="community.actions.confirm"
              >
                {t(`community.${chosen}.yes`)}
              </button>
            </div>
          )}
          {code !== null && (
            <p role="alert" className="text-sm text-red-800" data-testid="community.actions.error">
              {error(code).title}
            </p>
          )}
          <button
            type="button"
            className="min-h-11 rounded border border-gray-300 px-4"
            disabled={busy}
            onClick={close}
            data-testid="community.actions.cancel"
          >
            {t('community.cancel')}
          </button>
        </Sheet>
      )}
    </>
  );
}
