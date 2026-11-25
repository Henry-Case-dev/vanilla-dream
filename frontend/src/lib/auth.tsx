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
  const authEpoch = useRef(0);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  useEffect(() => {
    onUnauthorized(() => {
      authEpoch.current += 1;
      setCsrfToken(null);
      setState('guest');
    });
    return () => onUnauthorized(null);
  }, []);

  const refresh = useCallback(async () => {
    const epoch = authEpoch.current;
    try {
      const result = await api.auth.me();
      if (!mounted.current || epoch !== authEpoch.current) return;
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
      if (!mounted.current || epoch !== authEpoch.current) return;
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
    // Инвалидирует уже запущенный /api/auth/me, чтобы запоздавший ответ
    // «guest» не перетёр только что установленную сессию.
    authEpoch.current += 1;
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
    authEpoch.current += 1;
    setCsrfToken(null);
    setState('guest');
    setBootError(null);
    try {
      // Индикаторы операций принадлежат сессии владельца, а не браузеру.
      window.localStorage.removeItem('vd.upload.batch');
      window.localStorage.removeItem('vd.export.job');
    } catch {
      /* приватный режим — нечего очищать */
    }
    // Полная навигация очищает данные всех провайдеров (live/audio/query-состояния).
    window.location.replace('/admin/login');
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
