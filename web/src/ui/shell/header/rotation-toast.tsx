// Toasts of the pending-rotation job (06-web.md section 11 job 4): every automatic rotation batch the browser sent is
// announced (polite live region) with the org's name and what it did; each toast closes by itself after
// `TOAST_MS` or with its button.
import { useEffect } from 'react';
import { dismissRotation, type RotationNotice } from '../../../core/store/slices/rotation';
import { orgEntry } from '../../../core/store/slices/orgs';
import { useStore } from '../../hooks/useStore';
import { useI18n } from '../../i18n';
import { useAppEnv } from '../context';
import { useDirectory } from '../../screens/tasks/hooks';

export const TOAST_MS = 10_000;

function Toast({ notice }: { readonly notice: RotationNotice }) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const verified = useStore((state) => orgEntry(state.orgs, notice.org).log !== null, app.store);
  const name = useDirectory(notice.org, verified).orgName ?? t('orgs.unnamed');
  useEffect(() => {
    const timer = setTimeout(() => dismissRotation(app.store, notice.id), TOAST_MS);
    return () => clearTimeout(timer);
  }, [app, notice.id]);
  return (
    <li
      className="pointer-events-auto flex max-w-full items-center gap-3 rounded bg-gray-900 px-4 py-2 text-white shadow"
      data-testid="rotation-toast.item"
    >
      <span>
        {notice.devices > 0
          ? t('orgs.rotation.devices', { org: name, devices: notice.devices, scopes: notice.scopes })
          : t('orgs.rotation.scopes', { org: name, scopes: notice.scopes })}
      </span>
      <button
        type="button"
        className="min-h-11 min-w-11 underline"
        onClick={() => dismissRotation(app.store, notice.id)}
        aria-label={t('orgs.rotation.close')}
        data-testid="rotation-toast.close"
      >
        ×
      </button>
    </li>
  );
}

export function RotationToasts() {
  const { app } = useAppEnv();
  const notices = useStore((state) => state.rotation.notices, app.store);
  return (
    <ul
      aria-live="polite"
      className="fixed bottom-16 left-4 right-4 pointer-events-none z-30 flex flex-col items-end gap-2 md:bottom-4 md:left-auto"
      data-testid="rotation-toast"
    >
      {notices.map((notice) => (
        <Toast key={notice.id} notice={notice} />
      ))}
    </ul>
  );
}

export const headerItem = { id: 'rotation-toast', order: 90, Component: RotationToasts };
