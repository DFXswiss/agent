// A lazily loaded part of a screen (Markdown, chart, terminal) whose chunk cannot be loaded – the link to the hub is down
// at that moment, or the bytes fail their integrity check (`core/assets/integrity.ts`, fail closed) – shows its plain
// fallback instead of failing the whole screen (ACC-FINAL decision 11). A browser keeps a failed module as failed until
// the page reloads, so the fallback stays; nothing of the refused chunk runs. Any other error (a bug of the loaded code)
// passes on to the route's error page. Whole screens loaded lazily keep the error page with its reload button.
import { Component, type ReactNode } from 'react';
import { isChunkUnavailable } from '../../core/assets/integrity';

export interface ChunkBoundaryProps {
  readonly fallback: ReactNode;
  readonly children: ReactNode;
}

export class ChunkBoundary extends Component<ChunkBoundaryProps, { readonly error: unknown }> {
  override state: { readonly error: unknown } = { error: undefined };

  static getDerivedStateFromError(error: unknown): { readonly error: unknown } {
    return { error };
  }

  override render(): ReactNode {
    const { error } = this.state;
    if (error === undefined) return this.props.children;
    if (isChunkUnavailable(error)) return this.props.fallback;
    throw error;
  }
}
