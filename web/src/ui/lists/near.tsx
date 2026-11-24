// Heavy row content only near the viewport (ADR-086 section 1): Markdown, patches, diffs and JSON of a list row are
// built once the row comes within one screen of the viewport; until then the row shows a light one-line placeholder
// of the same text. One shared `IntersectionObserver` serves every row. Once shown, the content stays while the row
// is rendered (the window bounds the rows; dropping content above the viewport would move the page under the reader
// in browsers without scroll anchoring). Without `IntersectionObserver` everything is shown.
import { type ReactNode, useCallback, useRef, useState } from 'react';
import { oneLine } from '../../core/lists/text';

/** One screen above and below the viewport. */
export const NEAR_MARGIN = '100% 0px';

const waiting = new Map<Element, () => void>();
let shared: IntersectionObserver | null = null;

function observer(): IntersectionObserver {
  shared ??= new IntersectionObserver(
    (entries) => {
      for (const entry of entries) if (entry.isIntersecting) waiting.get(entry.target)?.();
    },
    { rootMargin: NEAR_MARGIN },
  );
  return shared;
}

/** Forgets the shared observer (tests replace `IntersectionObserver`). */
export function resetNearViewport(): void {
  shared = null;
  waiting.clear();
}

/** A ref for the row element and whether it has come near the viewport. */
export function useNearViewport<T extends Element>(): readonly [(node: T | null) => void, boolean] {
  const [near, setNear] = useState(() => typeof IntersectionObserver === 'undefined');
  const current = useRef<T | null>(null);
  const ref = useCallback(
    (node: T | null) => {
      const previous = current.current;
      // only an element still waiting is observed
      if (previous !== null && waiting.delete(previous)) shared!.unobserve(previous);
      current.current = node;
      if (node === null || near) return;
      waiting.set(node, () => {
        waiting.delete(node);
        observer().unobserve(node);
        setNear(true);
      });
      observer().observe(node);
    },
    [near],
  );
  return [ref, near] as const;
}

/** `children` once near the viewport, before that the first line of `text`. */
export function Near({
  text,
  className,
  children,
}: {
  readonly text: string;
  readonly className?: string;
  readonly children: ReactNode;
}) {
  const [ref, near] = useNearViewport<HTMLDivElement>();
  return (
    <div ref={ref} className={className} data-near={near}>
      {near ? (
        children
      ) : (
        <p className="truncate text-gray-600" data-testid="list.placeholder">
          {oneLine(text)}
        </p>
      )}
    </div>
  );
}
