// U-WEB-ui-chunk-boundary (ACC-FINAL decision 11): a lazily loaded part whose chunk cannot be loaded – the link to the
// hub is down while the chat or a board renders its first Markdown, or the bytes fail their integrity check – shows its
// plain fallback instead of failing the whole screen; nothing of the refused chunk runs.
import { render, screen } from '@testing-library/react';
import { Component, lazy, type ReactNode, Suspense } from 'react';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { ChunkUnavailable } from '../../src/core/assets/integrity';
import { ChunkBoundary } from '../../src/ui/lazy/ChunkBoundary';

afterEach(() => vi.restoreAllMocks());

describe('chunk boundary', () => {
  it('renders the fallback when the chunk cannot be loaded', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined); // React reports the caught error
    const Refused = lazy(() =>
      Promise.reject(new ChunkUnavailable('integrity check failed for /assets/Markdown-x.js')),
    );
    render(
      <ChunkBoundary fallback={<p>plain text</p>}>
        <Suspense fallback={<p>loading</p>}>
          <Refused />
        </Suspense>
      </ChunkBoundary>,
    );
    expect(await screen.findByText('plain text')).toBeTruthy();
    expect(screen.queryByText('loading')).toBeNull();
  });

  it('renders the loaded part when the chunk loads', async () => {
    const Loaded = lazy(() => Promise.resolve({ default: () => <p>formatted</p> }));
    render(
      <ChunkBoundary fallback={<p>plain text</p>}>
        <Suspense fallback={<p>loading</p>}>
          <Loaded />
        </Suspense>
      </ChunkBoundary>,
    );
    expect(await screen.findByText('formatted')).toBeTruthy();
  });
});

describe('chunk boundary and other errors', () => {
  it('lets an error that is not an unavailable chunk through to the next boundary', async () => {
    vi.spyOn(console, 'error').mockImplementation(() => undefined);
    const Broken = (): never => {
      throw new Error('render bug');
    };
    render(
      <Outer>
        <ChunkBoundary fallback={<p>plain text</p>}>
          <Broken />
        </ChunkBoundary>
      </Outer>,
    );
    expect(await screen.findByText('outer: render bug')).toBeTruthy();
    expect(screen.queryByText('plain text')).toBeNull();
  });
});

class Outer extends Component<{ readonly children: ReactNode }, { readonly error: string | null }> {
  override state = { error: null as string | null };
  static getDerivedStateFromError(error: Error): { readonly error: string } {
    return { error: error.message };
  }
  override render(): ReactNode {
    return this.state.error === null ? this.props.children : <p>outer: {this.state.error}</p>;
  }
}
