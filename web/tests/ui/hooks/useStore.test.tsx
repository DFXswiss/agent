// useStore(selector) re-renders on changes of the selected value only.
import { act, render, screen } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import { appStore, createStore } from '../../../src/core/store';
import { setOnline } from '../../../src/core/store/slices/connection';
import { useStore } from '../../../src/ui/hooks/useStore';

describe('useStore', () => {
  it('reads through a selector and re-renders on change', () => {
    const store = createStore();
    let renders = 0;
    function Online() {
      renders += 1;
      const online = useStore((state) => state.connection.online, store);
      return <p>{online ? 'online' : 'offline'}</p>;
    }
    render(<Online />);
    expect(screen.getByText('online')).toBeInTheDocument();
    act(() => setOnline(store, false));
    expect(screen.getByText('offline')).toBeInTheDocument();
    const count = renders;
    act(() => store.update('me', (me) => ({ ...me, userId: 'u' })));
    expect(renders).toBe(count);
  });

  it('uses the application store by default', () => {
    function Status() {
      return <p>{useStore((state) => state.connection.status)}</p>;
    }
    render(<Status />);
    expect(screen.getByText(appStore.getState().connection.status)).toBeInTheDocument();
  });
});
