// Toasts of the credential re-wrap job (06-web.md section 11 job 5): every automatic rotation of the credential key
// `CK` (a device lost access to the user's credentials) is announced in a polite live region with the number of
// re-encrypted records and of records of that device that were deleted; a rotation blocked by records the browser
// cannot open names them (the user deletes or replaces them in /settings). Each toast closes by itself after
// `TOAST_MS` or with its button.
import { useEffect } from 'react';
import { type CredentialRotationNotice, dismissCredentialRotation } from '../../../core/store/slices/credentials';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../context';

export const TOAST_MS = 10_000;

function Toast({ notice }: { readonly notice: CredentialRotationNotice }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const { result } = notice;
  useEffect(() => {
    const timer = setTimeout(() => dismissCredentialRotation(app.store, notice.id), TOAST_MS);
    return () => clearTimeout(timer);
  }, [app, notice.id]);
  return (
    <li
      className="pointer-events-auto flex max-w-full items-center gap-3 rounded bg-gray-900 px-4 py-2 text-white shadow"
      data-testid="credential-rotation.item"
    >
      <span data-status={result.status}>
        {result.status === 'blocked'
          ? t('credentials.rotation.blocked', {
              kinds: result.kinds.map((kind) => t(`credentials.kind.${kind}`)).join(', '),
            })
          : t('credentials.rotation.done', { records: result.records })}
        {result.status === 'rotated' &&
          result.dropped > 0 &&
          ` ${t('credentials.rotation.dropped', { dropped: result.dropped })}`}
      </span>
      <button
        type="button"
        className="min-h-11 min-w-11 underline"
        onClick={() => dismissCredentialRotation(app.store, notice.id)}
        aria-label={t('credentials.rotation.close')}
        data-testid="credential-rotation.close"
      >
        ×
      </button>
    </li>
  );
}

export function CredentialRotationToasts() {
  const { app } = useAppEnv();
  const notices = useStore((state) => state.credentials.notices, app.store);
  return (
    <ul
      aria-live="polite"
      className="fixed bottom-32 left-4 right-4 pointer-events-none z-30 flex flex-col items-end gap-2 md:bottom-20 md:left-auto"
      data-testid="credential-rotation"
    >
      {notices.map((notice) => (
        <Toast key={notice.id} notice={notice} />
      ))}
    </ul>
  );
}

export const headerItem = { id: 'credentials', order: 91, Component: CredentialRotationToasts };
