// A modal dialog that is a full-screen sheet below `md` and a centred card from `md` (06-web.md sections 12.1 and
// 12.3): labelled by its title, keyboard operable, focus trapped inside while open, Escape closes it unless it is busy;
// focus moves to the first control on open and back to the opener on close.
import { type ReactNode, useId, useRef } from 'react';
import { trapKey, useInitialFocus } from '../a11y/focus';

export interface SheetProps {
  readonly title: string;
  readonly testId: string;
  /** Closing (Escape) is ignored while busy. */
  readonly busy?: boolean;
  readonly onClose: () => void;
  readonly children: ReactNode;
}

export function Sheet({ title, testId, busy = false, onClose, children }: SheetProps) {
  const titleId = useId();
  const box = useRef<HTMLDivElement>(null);
  useInitialFocus(box);
  return (
    <div className="fixed inset-0 z-40 flex items-stretch justify-center bg-black/40 md:items-center md:p-4">
      <div
        ref={box}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        className="flex h-full w-full flex-col gap-4 overflow-y-auto bg-white p-4 md:h-auto md:max-h-full md:max-w-lg md:rounded md:p-6"
        onKeyDown={(event) => trapKey(event, box.current!, () => (busy ? undefined : onClose()))}
        data-testid={testId}
      >
        <h2 id={titleId} className="text-lg font-semibold">
          {title}
        </h2>
        {children}
      </div>
    </div>
  );
}
