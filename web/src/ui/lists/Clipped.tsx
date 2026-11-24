// A large single entry (ADR-063 section 1, the HOT-05 clip generalised): at most `CLIP_CHARS` characters at a time,
// "Show more" adds as many, and "Download" saves the full text as a file created in the browser from the decrypted
// value (a `blob:` URL opened by a download link: a download is no fetch and no navigation of the page, so the CSP of
// 06-web.md section 10.2 does not restrict it). Nothing of the text is sent anywhere.
import { useState } from 'react';
import { formatNumber } from '../../core/i18n';
import { clip, CLIP_CHARS } from '../../core/lists/text';
import { useI18n } from '../i18n';

export { CLIP_CHARS } from '../../core/lists/text';

export const MORE = 'min-h-11 self-start py-2 text-left text-sm text-blue-800 underline';

/** How long the `blob:` URL of a download stays valid (the browser has started the download by then). */
export const DOWNLOAD_MS = 60_000;

/** Saves `text` as the file `name` (plain text or JSON). */
export function download(text: string, name: string): void {
  const type = name.endsWith('.json') ? 'application/json' : 'text/plain';
  const url = URL.createObjectURL(new Blob([text], { type: `${type};charset=utf-8` }));
  const link = document.createElement('a');
  link.href = url;
  link.download = name;
  link.click();
  setTimeout(() => URL.revokeObjectURL(url), DOWNLOAD_MS);
}

export interface ClipOptions {
  /** Prefix of the buttons' test ids: `<controls>.more`, `<controls>.download`. */
  readonly controls: string;
  /** File name of "Download". */
  readonly file?: string;
}

/** `text` cut to the current limit, and while there is more the "Show more" and "Download" buttons. */
export function useClipped(
  text: string,
  { controls, file = 'text.txt' }: ClipOptions,
): { readonly shown: string; readonly more: JSX.Element | null } {
  const { t, locale } = useI18n();
  const [limit, setLimit] = useState(CLIP_CHARS);
  return {
    shown: clip(text, limit),
    more:
      text.length > limit ? (
        <span className="flex flex-wrap gap-x-4">
          <button
            type="button"
            className={MORE}
            onClick={() => setLimit(limit + CLIP_CHARS)}
            data-testid={`${controls}.more`}
          >
            {t('lists.clip.more', { count: formatNumber(text.length - limit, locale) })}
          </button>
          <button
            type="button"
            className={MORE}
            onClick={() => download(text, file)}
            data-testid={`${controls}.download`}
          >
            {t('lists.clip.download')}
          </button>
        </span>
      ) : null,
  };
}

export function Clipped({
  text,
  className,
  testId,
  controls,
  file,
}: {
  readonly text: string;
  readonly className: string;
  readonly testId: string;
  readonly controls: string;
  readonly file?: string;
}) {
  const { shown, more } = useClipped(text, file === undefined ? { controls } : { controls, file });
  return (
    <>
      <pre className={className} data-testid={testId}>
        {shown}
      </pre>
      {more}
    </>
  );
}
