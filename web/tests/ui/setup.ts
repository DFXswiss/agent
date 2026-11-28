import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeAll } from 'vitest';
import { preloadScreens } from '../../src/ui/lazy/screen';
import { FakeResizeObserver } from './resize-observer';

// Screens are separate chunks in the build (src/ui/lazy/screen.tsx); the tests render them synchronously.
beforeAll(() => preloadScreens());
// jsdom has no ResizeObserver (the terminal refits when its space changes)
globalThis.ResizeObserver ??= FakeResizeObserver as unknown as typeof ResizeObserver;
afterEach(() => cleanup());
