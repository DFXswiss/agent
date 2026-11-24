// The line diff of two versions (core/whiteboards/diff.ts) as a monospace block: added lines green with `+`, deleted
// lines red with `−`, unchanged lines plain; the counts as a summary. Not colour alone: every line carries its sign.
import { useMemo } from 'react';
import { diffLines, diffStats } from '../../core/whiteboards/diff';
import { useI18n } from '../i18n';

const STYLE = { same: 'text-gray-700', add: 'bg-green-50 text-green-900', del: 'bg-red-50 text-red-900' } as const;
const SIGN = { same: ' ', add: '+', del: '−' } as const;

export function LineDiff({ before, after }: { readonly before: string; readonly after: string }) {
  const { t } = useI18n();
  const lines = useMemo(() => diffLines(before, after), [before, after]);
  const stats = diffStats(lines);
  return (
    <figure className="flex flex-col gap-1" data-testid="whiteboard.diff">
      <figcaption className="text-xs text-gray-600" data-testid="whiteboard.diff.stats">
        {t('whiteboard.diff.stats', { added: stats.added, deleted: stats.deleted })}
      </figcaption>
      <pre className="overflow-x-auto rounded border border-gray-200 text-sm">
        {lines.map((line, index) => (
          <div key={index} className={STYLE[line.op]} data-testid="whiteboard.diff.line" data-op={line.op}>
            {`${SIGN[line.op]} ${line.text}`}
          </div>
        ))}
      </pre>
    </figure>
  );
}
