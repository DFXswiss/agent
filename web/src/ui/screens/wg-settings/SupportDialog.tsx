// The confirmation of a release change (ADR-096 section 5, workgroup settings): before a release
// (and a change of its access) what support then sees, the current support persons, that what support has read stays
// read after a revocation (idea 30), that tasks on DFX devices are processed there in clear independently of the
// release (A7) and that releasing again hands over the whole history again; before a revocation what it does.
import '../../../core/i18n/lazy/support';
import type { Access } from '../../../core/store/slices/support';
import { useI18n } from '../../i18n';
import { BUTTON, DANGER, PRIMARY } from '../members/ActionDialog';
import { Dialog } from '../members/Dialog';

/** What the dialog confirms: a new release, a change of its access, or the revocation. */
export type SupportChange =
  | { readonly kind: 'grant'; readonly access: Access }
  | { readonly kind: 'change'; readonly access: Access }
  | { readonly kind: 'revoke' };

export interface SupportDialogProps {
  readonly change: SupportChange;
  /** The current support persons, as shown in the section. */
  readonly people: string;
  readonly onConfirm: () => void;
  readonly onCancel: () => void;
}

export function SupportDialog({ change, people, onConfirm, onCancel }: SupportDialogProps) {
  const { t } = useI18n();
  const revoke = change.kind === 'revoke';
  return (
    <Dialog title={t(`support.dialog.${change.kind}.title`)} testId="support.dialog" onClose={onCancel}>
      {revoke ? (
        <>
          <p data-testid="support.dialog.revoke">{t('support.dialog.revoke.body')}</p>
          <p data-testid="support.dialog.kept">{t('support.dialog.kept')}</p>
        </>
      ) : (
        <>
          <p className="font-medium" data-testid="support.dialog.access">
            {t(`support.dialog.access.${change.access}`)}
          </p>
          <p data-testid="support.dialog.sees">{t('support.dialog.sees')}</p>
          <p data-testid="support.dialog.people">{people}</p>
          <p data-testid="support.dialog.kept">{t('support.dialog.kept')}</p>
          <p data-testid="support.dialog.devices">{t('support.dialog.devices')}</p>
          <p className="text-sm text-gray-700" data-testid="support.dialog.again">
            {t('support.dialog.again')}
          </p>
        </>
      )}
      <div className="flex flex-wrap gap-2">
        <button
          type="button"
          className={revoke ? DANGER : PRIMARY}
          onClick={onConfirm}
          data-testid="support.dialog.confirm"
        >
          {t(`support.dialog.confirm.${change.kind}`)}
        </button>
        <button type="button" className={BUTTON} onClick={onCancel} data-testid="support.dialog.cancel">
          {t('support.dialog.cancel')}
        </button>
      </div>
    </Dialog>
  );
}
