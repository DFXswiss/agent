// React access to the i18n of src/core/i18n: `useI18n()` returns the active `I18n` (`t`, `error`, `locale`).
import { createContext, useContext } from 'react';
import { createI18n, DEFAULT_LOCALE, type I18n } from '../core/i18n';

const I18nContext = createContext<I18n>(createI18n(DEFAULT_LOCALE));

export const I18nProvider = I18nContext.Provider;

export function useI18n(): I18n {
  return useContext(I18nContext);
}
