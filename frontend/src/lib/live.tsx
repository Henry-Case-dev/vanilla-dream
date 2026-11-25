import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { ApiError, api, errorMessage, normalizeNowPlaying } from './api';
import { useDataChanged } from './refresh';
import { useAuth } from './auth';
import type { NowPlaying, StationState } from './types';

export type FeedState = 'connecting' | 'live' | 'polling';

interface LiveContextValue {
  now: NowPlaying | null;
  control: StationState | null;
  loading: boolean;
  error: string | null;
  controlError: string | null;
  feed: FeedState;
  /** Монотонная ревизия состояния станции (SSE важнее старого HTTP). */
  revision: number;
  queueRevision: number | null;
  historyRevision: number | null;
  reload: () => void;
  reloadControl: () => void;
}

const LiveContext = createContext<LiveContextValue | null>(null);

/** Старый HTTP-ответ не должен перетирать более свежий SSE. */
export function isStaleHttp(currentRevision: number, incomingRevision: number): boolean {
  return incomingRevision < currentRevision;
}

/** Единый источник текущего эфира и состояния управления. */
export function LiveProvider({ children }: { children: ReactNode }) {
  const { state: authState } = useAuth();
  const [now, setNow] = useState<NowPlaying | null>(null);
  const [control, setControl] = useState<StationState | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [controlError, setControlError] = useState<string | null>(null);
  const [feed, setFeed] = useState<FeedState>('connecting');
  const disposed = useRef(false);
  const revisionRef = useRef(0);
  const controlRef = useRef<StationState | null>(null);
  const mountedControlRequest = useRef(0);

  const applyNow = useCallback((raw: unknown, source: 'sse' | 'http') => {
    const parsed = normalizeNowPlaying(raw);
    if (source === 'http' && isStaleHttp(revisionRef.current, parsed.station.revision)) {
      return;
    }
    revisionRef.current = Math.max(revisionRef.current, parsed.station.revision);
    setNow(parsed);
    setError(null);
    setLoading(false);
  }, []);

  const loadOnce = useCallback(async () => {
    try {
      const data = await api.public.nowPlaying();
      if (disposed.current) return;
      // api returns already-normalized data; the raw route is only used by SSE.
      if (isStaleHttp(revisionRef.current, data.station.revision)) return;
      revisionRef.current = Math.max(revisionRef.current, data.station.revision);
      setNow(data);
      setError(null);
      setLoading(false);
    } catch (caught) {
      if (disposed.current) return;
      setError((previous) => previous ?? errorMessage(caught));
      setLoading(false);
    }
  }, []);

  const reloadControl = useCallback(async () => {
    if (authState !== 'authenticated') return;
    const requestId = ++mountedControlRequest.current;
    try {
      const next = await api.admin.station.state();
      if (disposed.current || requestId !== mountedControlRequest.current) return;
      if (isStaleHttp(revisionRef.current, next.revision)) return;
      revisionRef.current = Math.max(revisionRef.current, next.revision);
      controlRef.current = next;
      setControl(next);
      setControlError(null);
    } catch (caught) {
      if (disposed.current || requestId !== mountedControlRequest.current) return;
      if (caught instanceof ApiError && caught.status === 404) {
        // Старый backend без state endpoint: работаем по публичному station.
        setControl(null);
        setControlError(null);
        return;
      }
      setControlError(errorMessage(caught));
    }
  }, [authState]);

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
          applyNow(JSON.parse(event.data) as unknown, 'sse');
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
        reconnectTimer = window.setTimeout(() => {
          reconnectTimer = null;
          connect();
        }, Math.min(30000, 4000 * attempts));
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
  }, [applyNow, loadOnce]);

  // Админское состояние (режим, pending, реальный override) — только в панели.
  useEffect(() => {
    if (authState === 'authenticated') void reloadControl();
  }, [authState, reloadControl]);

  const stationRevision = now?.station.revision ?? 0;
  useEffect(() => {
    if (authState !== 'authenticated') return;
    if (controlRef.current && controlRef.current.revision >= stationRevision) return;
    void reloadControl();
  }, [authState, stationRevision, reloadControl]);

  useEffect(() => {
    if (authState !== 'authenticated') return;
    const timer = window.setInterval(() => void reloadControl(), 15000);
    return () => window.clearInterval(timer);
  }, [authState, reloadControl]);

  const reload = useCallback(() => {
    void loadOnce();
    void reloadControl();
  }, [loadOnce, reloadControl]);

  useDataChanged(() => {
    void loadOnce();
    void reloadControl();
  });

  const value = useMemo<LiveContextValue>(
    () => ({
      now,
      control,
      loading,
      error,
      controlError,
      feed,
      revision: Math.max(revisionRef.current, now?.station.revision ?? 0),
      queueRevision: now?.queueRevision ?? null,
      historyRevision: now?.historyRevision ?? null,
      reload,
      reloadControl: () => void reloadControl(),
    }),
    [now, control, loading, error, controlError, feed, reload, reloadControl],
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

/** Состояние управления с публичным fallback, когда endpoint ещё недоступен. */
export function effectiveControl(
  control: StationState | null,
  now: NowPlaying | null,
): StationState | null {
  if (control) return control;
  if (!now) return null;
  return {
    mode: now.station.mode,
    selectedPlaylistId: now.station.programPlaylistId ?? now.station.effectiveSource?.playlistId ?? null,
    programPlaylistId: now.station.programPlaylistId,
    effectiveSource: now.station.effectiveSource,
    pendingSource: now.station.pendingSource,
    override: now.station.override,
    desired: now.station.desired,
    status: now.station.status,
    reason: now.station.reason,
    repeatGovernedBy: now.station.repeatGovernedBy,
    engineInstance: null,
    revision: now.station.revision,
    appliedRevision: null,
    notice: now.station.notice,
    diagnosticReason: now.station.diagnosticReason,
  };
}
