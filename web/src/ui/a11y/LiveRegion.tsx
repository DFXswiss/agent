// A visually hidden polite live region (06-web.md section 12.3): screen readers announce changes of its text (connection
// state, expiry of a device code) without interrupting.
export interface LiveRegionProps {
  readonly text: string;
  readonly testId?: string;
}

export function LiveRegion({ text, testId }: LiveRegionProps) {
  return (
    <p className="sr-only" aria-live="polite" aria-atomic="true" data-testid={testId}>
      {text}
    </p>
  );
}
