// U-WEB-ui-lists-heavy (ADR-063 section 1): a large single entry shows `CLIP_CHARS` characters at a time with "Show
// more" and "Download" (a file made in the browser), and heavy row content is built only near the viewport.
import { act, fireEvent, render, screen } from '@testing-library/react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { Clipped, CLIP_CHARS, DOWNLOAD_MS, useClipped } from '../../../src/ui/lists/Clipped';
import { Near, NEAR_MARGIN, resetNearViewport } from '../../../src/ui/lists/near';

describe('Clipped', () => {
  it('shows the text in steps and downloads the full text', () => {
    vi.useFakeTimers();
    const create = vi.fn(() => 'blob:test');
    const revoke = vi.fn();
    URL.createObjectURL = create;
    URL.revokeObjectURL = revoke;
    const click = vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    try {
      const text = 'z'.repeat(CLIP_CHARS * 2 + 3);
      render(<Clipped text={text} className="box" testId="detail.json" controls="detail" file="detail.json" />);
      expect(screen.getByTestId('detail.json').textContent).toHaveLength(CLIP_CHARS);
      expect(screen.getByTestId('detail.more')).toHaveTextContent('Show more (20,003 more characters)');
      fireEvent.click(screen.getByTestId('detail.download'));
      const blob = (create.mock.calls[0] as unknown as [Blob])[0];
      expect(blob.type).toBe('application/json;charset=utf-8');
      expect(blob.size).toBe(text.length);
      const link = click.mock.instances[0] as unknown as HTMLAnchorElement;
      expect(link.download).toBe('detail.json');
      expect(link.href).toBe('blob:test');
      expect(revoke).not.toHaveBeenCalled();
      vi.advanceTimersByTime(DOWNLOAD_MS);
      expect(revoke).toHaveBeenCalledWith('blob:test');
      fireEvent.click(screen.getByTestId('detail.more'));
      fireEvent.click(screen.getByTestId('detail.more'));
      expect(screen.getByTestId('detail.json').textContent).toBe(text);
      expect(screen.queryByTestId('detail.more')).toBeNull();
      expect(screen.queryByTestId('detail.download')).toBeNull();
    } finally {
      vi.useRealTimers();
    }
  });

  it('downloads plain text by default and shows short texts whole', () => {
    const create = vi.fn((_blob: Blob) => 'blob:plain');
    URL.createObjectURL = create;
    URL.revokeObjectURL = vi.fn();
    vi.spyOn(HTMLAnchorElement.prototype, 'click').mockImplementation(() => undefined);
    function Probe({ text }: { readonly text: string }) {
      const { shown, more } = useClipped(text, { controls: 'probe' });
      return (
        <div>
          <p data-testid="probe.text">{shown}</p>
          {more}
        </div>
      );
    }
    const view = render(<Probe text="short" />);
    expect(screen.getByTestId('probe.text')).toHaveTextContent('short');
    expect(screen.queryByTestId('probe.more')).toBeNull();
    view.rerender(<Probe text={'q'.repeat(CLIP_CHARS + 1)} />);
    fireEvent.click(screen.getByTestId('probe.download'));
    expect(create.mock.calls[0]![0].type).toBe('text/plain;charset=utf-8');
    render(<Clipped text="x" className="c" testId="plain" controls="plain" />);
    expect(screen.getByTestId('plain')).toHaveTextContent('x');
  });
});

describe('Near', () => {
  const original = globalThis.IntersectionObserver;
  afterEach(() => {
    globalThis.IntersectionObserver = original;
    resetNearViewport();
  });

  it('renders everything where IntersectionObserver is missing', () => {
    // jsdom has none
    expect(typeof IntersectionObserver).toBe('undefined');
    const view = render(
      <Near text="heavy">
        <b data-testid="heavy">content</b>
      </Near>,
    );
    expect(screen.getByTestId('heavy')).toBeInTheDocument();
    view.unmount();
  });

  it('shows a one-line placeholder until the row comes near the viewport', () => {
    const observed = new Set<Element>();
    let callback: IntersectionObserverCallback = () => undefined;
    const options: IntersectionObserverInit[] = [];
    class FakeObserver {
      constructor(cb: IntersectionObserverCallback, init: IntersectionObserverInit) {
        callback = cb;
        options.push(init);
      }
      observe(node: Element) {
        observed.add(node);
      }
      unobserve(node: Element) {
        observed.delete(node);
      }
    }
    globalThis.IntersectionObserver = FakeObserver as unknown as typeof IntersectionObserver;
    const fire = (target: Element, isIntersecting: boolean) =>
      act(() => callback([{ target, isIntersecting } as IntersectionObserverEntry], {} as IntersectionObserver));
    const view = render(
      <>
        <Near text={'first line\nsecond line'} className="row">
          <b data-testid="heavy">content</b>
        </Near>
        <Near text="other">
          <b data-testid="heavy.other">other</b>
        </Near>
      </>,
    );
    expect(options).toEqual([{ rootMargin: NEAR_MARGIN }]);
    expect(screen.queryByTestId('heavy')).toBeNull();
    const placeholders = screen.getAllByTestId('list.placeholder');
    expect(placeholders[0]).toHaveTextContent('first line second line');
    const [first, second] = [...observed];
    fire(first!, false);
    expect(screen.queryByTestId('heavy')).toBeNull();
    fire(first!, true);
    expect(screen.getByTestId('heavy')).toBeInTheDocument();
    expect(first).toHaveAttribute('data-near', 'true');
    expect(observed.has(first!)).toBe(false);
    // an entry for an element that no longer waits is ignored
    fire(first!, true);
    // unmounting a waiting row stops observing it
    view.unmount();
    expect(observed.has(second!)).toBe(false);
  });
});
