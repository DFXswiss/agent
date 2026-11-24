// The audit screen (06-web.md section 9.2, route `/o/:org/audit`, right org:member; an admin sees every row of the org,
// a member the rows of the workgroups it reads and its own actions, 03-api.md section 4.11). Admins verify the chain.
import { useParams } from 'react-router-dom';
import { useI18n } from '../../i18n';
import { AuditView } from './AuditView';

export function AuditScreen() {
  const { org } = useParams() as { org: string };
  const { t } = useI18n();
  return (
    <section className="flex flex-col gap-4 p-4" data-testid="audit.screen">
      <h1 className="text-xl font-semibold" data-testid="audit.title">
        {t('audit.title')}
      </h1>
      <AuditView org={org} task={null} listId="audit" />
    </section>
  );
}
