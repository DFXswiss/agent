// One mailbox with its actions (KOR-74, ADR-099 section 9): name, the mark of the primary one (the mailbox of the
// automatic login), address, purpose and the workgroups it is released to; change, release, check and delete (asked
// once). A record that does not open can only be deleted. The password is never shown.
import { useState } from 'react';
import { type Mailbox, removeMailbox } from '../../../../core/credentials/mailboxes';
import { requestErrorCode } from '../../../../core/requests/client';
import { useI18n } from '../../../i18n';
import { useAppEnv } from '../../../shell/context';
import { BUTTON, PRIMARY } from '../credentials/DevicePicker';
import { useReason } from '../github/reason';
import { MailboxCheck } from './MailboxCheck';
import { MailboxForm } from './MailboxForm';
import { Release, useReleasable, WorkgroupName } from './Release';

type Open = 'none' | 'edit' | 'release' | 'check' | 'remove';

function Released({ wgs }: { readonly wgs: readonly string[] }) {
  const { t } = useI18n();
  const releasable = useReleasable();
  if (wgs.length === 0) return <p data-testid="mbcard.released">{t('mailbox.released.none')}</p>;
  return (
    <div data-testid="mbcard.released">
      <p>{t('mailbox.released')}</p>
      <ul className="ml-4 list-disc">
        {wgs.map((wg) => (
          <li key={wg} data-testid={`mbcard.released.${wg}`}>
            <WorkgroupName org={releasable.get(wg)} wg={wg} />
          </li>
        ))}
      </ul>
    </div>
  );
}

export function MailboxCard({
  item,
  items,
  onChanged,
}: {
  readonly item: Mailbox;
  readonly items: readonly Mailbox[];
  readonly onChanged: () => void;
}) {
  const { app } = useAppEnv();
  const { t } = useI18n();
  const reason = useReason();
  const [open, setOpen] = useState<Open>('none');
  const [failure, setFailure] = useState<string | null>(null);
  const verified = item.status === 'verified';
  const remove = async () => {
    setFailure(null);
    try {
      await removeMailbox(app.tasks, item.id);
      setOpen('none');
      onChanged();
    } catch (error) {
      setFailure(requestErrorCode(error));
    }
  };
  const action = (to: Open, testId: string, text: string) => (
    <button type="button" className={BUTTON} onClick={() => setOpen(to)} data-testid={testId}>
      {text}
    </button>
  );
  return (
    <article
      className="flex flex-col gap-2 rounded border border-gray-300 p-3"
      data-testid={`mbcard.${item.id}`}
      data-name={item.name ?? ''}
    >
      <h4 className="font-semibold">{item.name ?? t('mailbox.unnamed')}</h4>
      {item.primary && (
        <p className="self-start rounded bg-gray-100 px-2 text-sm" data-testid="mbcard.primary">
          {t('mailbox.primary')}
        </p>
      )}
      {verified ? (
        <>
          <p className="text-sm" data-testid="mbcard.email">
            {item.email}
          </p>
          {item.purpose !== null && (
            <p className="text-sm text-gray-700" data-testid="mbcard.purpose">
              {item.purpose}
            </p>
          )}
          <Released wgs={item.wgs} />
        </>
      ) : (
        <p role="alert" data-testid="mbcard.unverifiable" data-code={item.code}>
          {t('mailbox.unverifiable', { reason: reason(String(item.code)) })}
        </p>
      )}
      {open === 'none' && (
        <div className="flex flex-wrap gap-2">
          {verified && action('edit', 'mbcard.edit', t('mailbox.edit'))}
          {verified && action('release', 'mbcard.release', t('mailbox.release'))}
          {verified && action('check', 'mbcard.check', t('mailbox.check'))}
          {action('remove', 'mbcard.remove', t('mailbox.remove'))}
        </div>
      )}
      {open === 'edit' && (
        <MailboxForm
          item={item}
          items={items}
          onSaved={() => {
            setOpen('none');
            onChanged();
          }}
          onClose={() => setOpen('none')}
        />
      )}
      {open === 'release' && (
        <>
          <Release item={item} onSaved={onChanged} />
          <button type="button" className={BUTTON} onClick={() => setOpen('none')} data-testid="mbcard.release.close">
            {t('mailbox.close')}
          </button>
        </>
      )}
      {open === 'check' && <MailboxCheck id={item.id} onClose={() => setOpen('none')} />}
      {open === 'remove' && (
        <div className="flex flex-col gap-2" role="alertdialog" data-testid="mbcard.confirm">
          <p>{t('mailbox.remove.confirm')}</p>
          <div className="flex flex-wrap gap-2">
            <button type="button" className={PRIMARY} onClick={() => void remove()} data-testid="mbcard.remove.yes">
              {t('mailbox.remove')}
            </button>
            <button type="button" className={BUTTON} onClick={() => setOpen('none')} data-testid="mbcard.remove.no">
              {t('mailbox.cancel')}
            </button>
          </div>
        </div>
      )}
      {failure !== null && (
        <p role="alert" data-testid="mbcard.failed" data-code={failure}>
          {t('mailbox.save-failed', { reason: reason(failure) })}
        </p>
      )}
    </article>
  );
}
