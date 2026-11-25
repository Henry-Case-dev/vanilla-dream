import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { api, errorMessage, normalizeNowPlaying } from './api';
import { useDataChanged } from './refresh';
import type { NowPlaying } from './types';

export type FeedState = 'connecting' | 'live' | 'polling';

interface LiveContextValue {
  now: NowPlaying | null;
  loading: boolean;
  error: string | null;
  feed: FeedState;
  reload: () => void;
}

const LiveContext = createContext<LiveContextValue | null>(null);

/**
 * Единый источник текущего эфира: SSE `/api/public/events` с резервным
 * экономным опросом `/api/public/now-playing`, если SSE недоступен.
 */
export function LiveProvider({ children }: { children: ReactNode }) {
  const [now, setNow] = useState<NowPlaying | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [feed, setFeed] = useState<FeedState>('connecting');
  const disposed = useRef(false);

  const applyRaw = useCallback((raw: unknown) => {
    setNow(normalizeNowPlaying(raw));
    setError(null);
    setLoading(false);
  }, []);

  const loadOnce = useCallback(async () => {
    try {
      const data = await api.public.nowPlaying();
      if (disposed.current) return;
      setNow(data);
      setError(null);
      setLoading(false);
    } catch (caught) {
      if (disposed.current) return;
      setError((previous) => previous ?? errorMessage(caught));
      setLoading(false);
    }
  }, []);

  useEffect(() => {
    disposed.current = false;
    let source: EventSource | null = null;
    let pollTimer: number | null = null;
    let reconnectTimer: number | null = null;
    let attempts = 0;

    const stopPolling = () => {
      if (pollTimer !== null) {
        window.clearInterval(pollTimer);
        pollTimer = null;
      }
    };

    const startPolling = () => {
      if (pollTimer !== null || disposed.current) return;
      pollTimer = window.setInterval(() => {
        void loadOnce();
      }, 6000);
    };

    const connect = () => {
      if (disposed.current) return;
      let next: EventSource;
      try {
        next = new EventSource('/api/public/events');
      } catch {
        setFeed('polling');
        startPolling();
        return;
      }
      source = next;

      next.onopen = () => {
        if (disposed.current) return;
        attempts = 0;
        setFeed('live');
        stopPolling();
      };

      next.onmessage = (event) => {
        if (!event.data || disposed.current) return;
        try {
          applyRaw(JSON.parse(event.data) as unknown);
          setFeed('live');
          stopPolling();
        } catch {
          /* служебный комментарий или неполный кадр — пропускаем */
        }
      };

      next.onerror = () => {
        next.close();
        if (source === next) source = null;
        if (disposed.current) return;
        attempts += 1;
        setFeed('polling');
        startPolling();
        void loadOnce();
        if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
        reconnectTimer = window.setTimeout(
          () => {
            reconnectTimer = null;
            connect();
          },
          Math.min(30000, 4000 * attempts),
        );
      };
    };

    void loadOnce();
    connect();

    const onVisibility = () => {
      if (document.visibilityState === 'visible') void loadOnce();
    };
    document.addEventListener('visibilitychange', onVisibility);

    return () => {
      disposed.current = true;
      document.removeEventListener('visibilitychange', onVisibility);
      if (source) source.close();
      stopPolling();
      if (reconnectTimer !== null) window.clearTimeout(reconnectTimer);
    };
  }, [applyRaw, loadOnce]);

  const reload = useCallback(() => {
    void loadOnce();
  }, [loadOnce]);

  useDataChanged(() => {
    void loadOnce();
  });

  const value = useMemo<LiveContextValue>(
    () => ({ now, loading, error, feed, reload }),
    [now, loading, error, feed, reload],
  );

  return <LiveContext.Provider value={value}>{children}</LiveContext.Provider>;
}

export function useLive(): LiveContextValue {
  const context = useContext(LiveContext);
  if (!context) throw new Error('LiveProvider отсутствует в дереве компонентов');
  return context;
}

export function useNowPlaying(): NowPlaying | null {
  return useLive().now;
}
