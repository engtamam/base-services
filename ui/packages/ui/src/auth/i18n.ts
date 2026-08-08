import { createContext, useContext } from 'react';

/** The auth i18n seam — the SAME pattern the block editor uses. Outside a provider it degrades to the
 *  key itself + English, so the components never import an app's dictionary. */
export interface AuthI18n { t: (key: string) => string; locale: 'en' | 'ar' }
export const AuthI18nContext = createContext<AuthI18n>({ t: (k) => k, locale: 'en' });
export function useAuthI18n(): AuthI18n { return useContext(AuthI18nContext); }
export const AuthI18nProvider = AuthI18nContext.Provider;
