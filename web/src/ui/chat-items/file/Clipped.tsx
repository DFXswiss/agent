// Long text of a chat item (HOT-05): at most `CLIP_CHARS` characters at a time, the rest behind "Show more" – a tool
// call with a megabyte of output or a very long patch must not stall the page.
import { useState } from 'react';
import { formatNumber } from '../../../core/i18n';
import { useI18n } from '../../i18n';

export const CLIP_CHARS = 20_000;

export const MORE = 'min-h-11 self-start py-2 text-left text-sm text-blue-800 underline';

/** `text` cut to the current limit and a "Show more" button while there is more. */
export function useClipped(text: string): { readonly shown: string; readonly more: JSX.Element | null } {
  const { t, locale } = useI18n();
  const [limit, setLimit] = useState(CLIP_CHARS);
  return {
    shown: text.length > limit ? text.slice(0, limit) : text,
    more:
      text.length > limit ? (
        <button type="button" className={MORE} onClick={() => setLimit(limit + CLIP_CHARS)} data-testid="chat.more">
          {t('chat-list.patch.more', { count: formatNumber(text.length - limit, locale) })}
        </button>
      ) : null,
  };
}

export function Clipped({
  text,
  className,
  testId,
}: {
  readonly text: string;
  readonly className: string;
  readonly testId: string;
}) {
  const { shown, more } = useClipped(text);
  return (
    <>
      <pre className={className} data-testid={testId}>
        {shown}
      </pre>
      {more}
    </>
  );
}
