// jsdom has no ResizeObserver: a double installed by `setup.ts`. `resized()` calls every observer that currently
// observes an element, as the browser does after a layout change.
const callbacks = new Set<() => void>();

export class FakeResizeObserver {
  private readonly callback: () => void;
  constructor(callback: () => void) {
    this.callback = () => callback();
  }
  observe(): void {
    callbacks.add(this.callback);
  }
  unobserve(): void {
    callbacks.delete(this.callback);
  }
  disconnect(): void {
    callbacks.delete(this.callback);
  }
}

/** Every observed element changed its size. */
export function resized(): void {
  for (const callback of [...callbacks]) callback();
}
