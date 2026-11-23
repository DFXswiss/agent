// "Skip to content" (06-web.md section 12.3): the first focusable element of every page; visible only while focused,
// it moves the keyboard focus past header and navigation to the screen.
import type { MouseEvent } from 'react';
import { useI18n } from '../i18n';

/** Moves the focus to the screen without changing the URL fragment (the invitation code lives there, section 2.4). */
export function skipToContent(event: MouseEvent<HTMLAnchorElement>): void {
  event.preventDefault();
  document.getElementById(CONTENT_ID)?.focus();
}

export const CONTENT_ID = 'content';

export function SkipLink() {
  const { t } = useI18n();
  return (
    <a
      href={`#${CONTENT_ID}`}
      className="sr-only focus:not-sr-only focus:absolute focus:left-2 focus:top-2 focus:z-30 focus:flex focus:min-h-11 focus:items-center focus:rounded focus:bg-white focus:px-4 focus:shadow"
      onClick={skipToContent}
      data-testid="shell.skip"
    >
      {t('shell.skip')}
    </a>
  );
}
