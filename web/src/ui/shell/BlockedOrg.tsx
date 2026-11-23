// Blocking error screen of an org whose log failed verification (06-web.md section 11 job 1, section 12.6): no reads
// or writes in that org; the evidence (stored head, served head, the event's `lh`) is shown for support.
import type { OrgBlock } from '../../core/store/slices/orgs';
import { ErrorLayout } from '../screens/errors/ErrorLayout';
import { useI18n } from '../i18n';

export function BlockedOrg({ block }: { readonly block: OrgBlock }) {
  const { t, error } = useI18n();
  const none = t('shell.blocked.none');
  const head = (value: OrgBlock['stored']) => (value === null ? none : `${value.seq}:${value.hash}`);
  return (
    <ErrorLayout
      testId="shell.blocked"
      prefix="shell.blocked"
      title={t('shell.blocked.title')}
      body={
        <div className="flex flex-col gap-4">
          <p>{t('shell.blocked.body')}</p>
          <p>{error(block.code).title}</p>
          <p className="break-all font-mono text-sm text-gray-700" data-testid="shell.blocked.evidence">
            {t('shell.blocked.evidence', {
              stored: head(block.stored),
              served: head(block.served),
              lh: block.lh === null ? none : block.lh,
            })}
          </p>
        </div>
      }
      action={error(block.code).action}
      code={block.code}
    />
  );
}
