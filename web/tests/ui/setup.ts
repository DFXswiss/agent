import '@testing-library/jest-dom/vitest';
import { cleanup } from '@testing-library/react';
import { afterEach, beforeAll } from 'vitest';
import { preloadScreens } from '../../src/ui/lazy/screen';

// Screens are separate chunks in the build (src/ui/lazy/screen.tsx); the tests render them synchronously.
beforeAll(() => preloadScreens());
afterEach(() => cleanup());
