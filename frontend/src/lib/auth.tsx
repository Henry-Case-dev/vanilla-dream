import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { api, errorMessage, onUnauthorized, setCsrfToken } from './api';

type AuthState = 'loading' | 'authenticated' | 'guest';

interface AuthContextValue {
  state: AuthState;
  bootError: string | null;
  login: (password: string) => Promise<void>;
  logout: () => Promise<void>;
  refresh: () => Promise<void>;
}

const AuthContext = createContext<AuthContextValue | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  const [state, setState] = useState<AuthState>('loading');
  const [bootError, setBootError] = useState<string | null>(null);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    onUnauthorized(() => {
      setCsrfToken(null);
      setState('guest');
    });
    return () => onUnauthorized(null);
  }, []);

  const refresh = useCallback(async () => {
    try {
      const result = await api.auth.me();
      if (!mounted.current) return;
      if (result.authenticated) {
        setCsrfToken(result.csrfToken);
        setState('authenticated');
        setBootError(null);
      } else {
        setCsrfToken(null);
        setState('guest');
        setBootError(null);
      }
    } catch (caught) {
      if (!mounted.current) return;
      setCsrfToken(null);
      setState('guest');
      setBootError(errorMessage(caught));
    }
  }, []);

  useEffect(() => {
    void refresh();
  }, [refresh]);

  const login = useCallback(async (password: string) => {
    const result = await api.auth.login(password);
    setCsrfToken(result.csrfToken);
    setState('authenticated');
    setBootError(null);
  }, []);

  const logout = useCallback(async () => {
    try {
      await api.auth.logout();
    } catch {
      /* даже при сетевой ошибке локально завершаем сессию */
    }
    setCsrfToken(null);
    setState('guest');
  }, []);

  const value = useMemo<AuthContextValue>(
    () => ({ state, bootError, login, logout, refresh }),
    [state, bootError, login, logout, refresh],
  );

  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

export function useAuth(): AuthContextValue {
  const context = useContext(AuthContext);
  if (!context) throw new Error('AuthProvider отсутствует в дереве компонентов');
  return context;
}

export function RequireAuth({ children }: { children: ReactNode }) {
  const { state } = useAuth();
  const location = useLocation();

  if (state === 'loading') {
    return (
      <div className="page-loader" role="status" aria-live="polite">
        <span className="spinner" aria-hidden="true" />
        <span>Проверка сессии…</span>
      </div>
    );
  }

  if (state === 'guest') {
    return (
      <Navigate
        to="/admin/login"
        replace
        state={{ from: `${location.pathname}${location.search}` }}
      />
    );
  }

  return <>{children}</>;
}
