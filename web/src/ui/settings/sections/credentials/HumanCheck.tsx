// A check for humans in the login sandbox (KOR-73, ADR-100 section 4): the device's newest picture of the sandbox; the
// owner clicks into it where they would click on the page, and the click goes to the device as fractions of the
// picture (`sendClick`). The owner does the check; the platform only carries the click. "Sign in myself" gives the
// automation up (`sendRelay`): the device answers with the link and the code continues as the relay.
import type { MouseEvent } from 'react';
import type { VerifyImage } from '../../../../core/requests/bodies';
import { useI18n } from '../../../i18n';
import { BUTTON } from './DevicePicker';
import '../../../../core/i18n/lazy/login-check';

/** The fraction of `offset` within `size` (the middle when the picture has no size yet). */
const fraction = (offset: number, size: number): number => (size > 0 ? offset / size : 0.5);

export function HumanCheck({
  frame,
  image,
  clicked,
  onClick,
  onRelay,
}: {
  readonly frame: number;
  readonly image: VerifyImage;
  readonly clicked: boolean;
  readonly onClick: (x: number, y: number) => void;
  readonly onRelay: () => void;
}) {
  const { t } = useI18n();
  const click = (event: MouseEvent<HTMLButtonElement>) => {
    // a key press has no point in the picture: only a pointer click is sent (else the relay)
    if (event.detail === 0) return;
    const box = event.currentTarget.getBoundingClientRect();
    onClick(fraction(event.clientX - box.left, box.width), fraction(event.clientY - box.top, box.height));
  };
  return (
    <section className="flex flex-col gap-2" data-testid="claude.verify" data-frame={frame}>
      <h5 className="font-semibold">{t('login-check.verify.title')}</h5>
      <p>{t('login-check.verify.intro')}</p>
      <button type="button" className="block w-full p-0" onClick={click} data-testid="claude.verify.picture">
        <img
          className="w-full rounded border border-gray-400"
          src={`data:image/jpeg;base64,${image.data}`}
          width={image.width}
          height={image.height}
          alt={t('login-check.verify.picture', { frame })}
        />
      </button>
      {clicked && (
        <p aria-live="polite" data-testid="claude.verify.clicked">
          {t('login-check.verify.clicked')}
        </p>
      )}
      <button type="button" className={BUTTON} onClick={onRelay} data-testid="claude.verify.relay">
        {t('login-check.verify.relay')}
      </button>
    </section>
  );
}
