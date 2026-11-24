// The calm "being unlocked" line of a workgroup (ADR-058 section 2, `core/org/unlock.ts`): shown above the content of
// the task list (metadata stays visible), and instead of the content of a task and a workgroup whiteboard (`children`),
// while the user may read the workgroup but holds no key of its current epoch; the dead-end variant when nobody with
// access is left to deliver it. Nothing to do for the user.
import type { ReactNode } from 'react';
import { selectUnlock } from '../../core/org/unlock';
import { useStore } from '../hooks/useStore';
import { useI18n } from '../i18n';
import { useAppEnv } from '../shell/context';

export function Unlocking(props: { readonly org: string; readonly wg: string; readonly children?: ReactNode }) {
  const { org, wg, children } = props;
  const { app } = useAppEnv();
  const { t } = useI18n();
  const status = useStore(selectUnlock(org, wg), app.store);
  if (status === null) return children;
  return (
    <p role="status" className="text-gray-700" data-testid="unlocking" data-status={status}>
      {status === 'lost' ? t('workgroups.unlocking.lost') : t('workgroups.unlocking')}
    </p>
  );
}
