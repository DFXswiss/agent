// The line diff of two versions (core/whiteboards/diff.ts) as a monospace block: only the changed hunks with three lines
// of context (`hunks.ts`), each under its `@@` header; added lines green with `+`, deleted lines red with `−`, context
// lines plain; the counts as a summary. Not colour alone: every line carries its sign. A very long diff (or line) is
// clipped (`useClipped`: "Show more", "Download"). Built only when mounted: the history mounts it once a version's
// diff is expanded.
import { useMemo } from 'react';
import { diffLines, diffStats } from '../../core/whiteboards/diff';
import { useI18n } from '../i18n';
import { useClipped } from '../lists/Clipped';
import { diffText, hunks, lineKind, type TextLineKind } from './hunks';

const STYLE: Readonly<Record<TextLineKind, string>> = {
  same: 'text-gray-700',
  add: 'bg-green-50 text-green-900',
  del: 'bg-red-50 text-red-900',
  hunk: 'bg-gray-100 text-gray-600',
};

export function LineDiff({ before, after }: { readonly before: string; readonly after: string }) {
  const { t } = useI18n();
  const { stats, text } = useMemo(() => {
    const lines = diffLines(before, after);
    return { stats: diffStats(lines), text: diffText(hunks(lines)) };
  }, [before, after]);
  const { shown, more } = useClipped(text, { controls: 'whiteboard.diff', file: 'whiteboard-diff.txt' });
  const rows = useMemo(() => shown.split('\n'), [shown]);
  return (
    <figure className="flex flex-col gap-1" data-testid="whiteboard.diff">
      <figcaption className="text-xs text-gray-600" data-testid="whiteboard.diff.stats">
        {t('whiteboard.diff.stats', { added: stats.added, deleted: stats.deleted })}
      </figcaption>
      {text === '' ? (
        <p className="text-sm text-gray-600" data-testid="whiteboard.diff.none">
          {t('whiteboard.diff.none')}
        </p>
      ) : (
        <pre className="overflow-x-auto rounded border border-gray-200 text-sm">
          {rows.map((line, index) => {
            const kind = lineKind(line);
            return (
              <div key={index} className={STYLE[kind]} data-testid="whiteboard.diff.line" data-op={kind}>
                {line}
              </div>
            );
          })}
        </pre>
      )}
      {more}
    </figure>
  );
}
