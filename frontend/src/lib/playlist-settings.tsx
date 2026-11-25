import {
  createContext,
  useCallback,
  useContext,
  useEffect,
  useMemo,
  useRef,
  useState,
} from 'react';
import type { ReactNode } from 'react';
import { ApiError, api, errorMessage } from './api';
import { useLive } from './live';
import { useAuth } from './auth';
import { useToast } from '../components/Toasts';
import { useDataChanged } from './refresh';
import type { ApplicationState, Playlist, PlaylistUpdateResult, RepeatMode } from './types';

type Field = 'shuffle' | 'repeat';

interface PlaylistSettingsValue {
  playlists: Playlist[] | null;
  loading: boolean;
  error: string | null;
  refresh: () => Promise<void>;
  get: (id: string | null | undefined) => Playlist | null;
  busy: (id: string | null | undefined, field: Field) => boolean;
  application: (id: string | null | undefined) => ApplicationState | null;
  setShuffle: (id: string, value: boolean) => Promise<void>;
  cycleRepeat: (id: string) => Promise<void>;
  setRepeat: (id: string, value: RepeatMode) => Promise<void>;
}

const PlaylistSettingsContext = createContext<PlaylistSettingsValue | null>(null);

function nextRepeat(mode: RepeatMode): RepeatMode {
  if (mode === 'none') return 'all';
  if (mode === 'all') return 'one';
  return 'none';
}

/**
 * Один кэш настроек плейлистов для нижней панели, страницы и карточек.
 * Изменения оптимистичны и сериализуются по каждому полю: поздний ответ
 * shuffle не может перетереть более новый repeat и наоборот.
 */
export function PlaylistSettingsProvider({ children }: { children: ReactNode }) {
  const { revision, control, reloadControl } = useLive();
  const { state: authState } = useAuth();
  const toast = useToast();
  const [playlists, setPlaylists] = useState<Playlist[] | null>(null);
  const [loading, setLoading] = useState(true);
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState<Record<string, Field | undefined>>({});
  const [applications, setApplications] = useState<
    Record<string, { application: ApplicationState; revision: number | null }>
  >({});
  const itemsRef = useRef<Playlist[] | null>(null);
  const revisionRef = useRef(revision);
  const loadId = useRef(0);
  const inFlight = useRef(0);
  const fieldSeq = useRef<Record<string, { shuffle: number; repeat: number }>>({});

  revisionRef.current = Math.max(revisionRef.current, revision);

  itemsRef.current = playlists;

  const apply = useCallback((id: string, patch: Partial<Playlist>) => {
    const current = itemsRef.current;
    if (!current) return;
    const next = current.map((item) => (item.id === id ? { ...item, ...patch } : item));
    // Синхронно, чтобы быстрый повторный клик считал следующий режим
    // уже от оптимистичного значения, а не от старого снимка.
    itemsRef.current = next;
    setPlaylists(next);
  }, []);

  const refresh = useCallback(async () => {
    if (authState !== 'authenticated') return;
    if (inFlight.current > 0) return;
    const requestId = ++loadId.current;
    const seqStart: Record<string, { shuffle: number; repeat: number }> = {};
    Object.entries(fieldSeq.current).forEach(([id, entry]) => {
      seqStart[id] = { ...entry };
    });
    setLoading(true);
    try {
      const list = await api.admin.playlists.list();
      if (requestId !== loadId.current) return;
      setPlaylists((current) => {
        if (!current) return list;
        return list.map((next) => {
          const previous = current.find((item) => item.id === next.id);
          if (!previous) return next;
          const before = seqStart[next.id] ?? { shuffle: 0, repeat: 0 };
          const after = fieldSeq.current[next.id] ?? { shuffle: 0, repeat: 0 };
          return {
            ...next,
            // Пока запрос летел, пользователь успел изменить поле — его
            // подтверждённое/оптимистичное значение важнее старого HTTP.
            shuffle: after.shuffle !== before.shuffle ? previous.shuffle : next.shuffle,
            repeatMode:
              after.repeat !== before.repeat ? previous.repeatMode : next.repeatMode,
            repeatGovernedBy:
              after.repeat !== before.repeat ? previous.repeatGovernedBy : next.repeatGovernedBy,
          };
        });
      });
      setError(null);
    } catch (caught) {
      if (requestId !== loadId.current) return;
      setError(errorMessage(caught));
    } finally {
      if (requestId === loadId.current) setLoading(false);
    }
  }, [authState]);

  // Ревизия CORE-03 приходит по SSE; не опрашиваем плейлисты раз в минуту.
  useEffect(() => {
    void refresh();
  }, [revision, refresh]);

  useDataChanged(() => {
    void refresh();
  });

  useEffect(() => {
    if (!control || control.appliedRevision === null) return;
    setApplications((current) => {
      let changed = false;
      const next = { ...current };
      Object.entries(current).forEach(([id, entry]) => {
        if (
          entry.application === 'pending' &&
          entry.revision !== null &&
          control.revision >= entry.revision &&
          control.appliedRevision !== null &&
          control.appliedRevision >= entry.revision
        ) {
          next[id] = { application: 'applied', revision: entry.revision };
          changed = true;
        }
      });
      return changed ? next : current;
    });
  }, [control?.revision, control?.appliedRevision]);

  // applied_revision может догнать state_revision без нового state_revision:
  // пока есть сохранённая, но не применённая настройка, перечитываем состояние.
  useEffect(() => {
    if (!Object.values(applications).some((entry) => entry.application === 'pending')) return;
    const timer = window.setInterval(() => reloadControl(), 1000);
    return () => window.clearInterval(timer);
  }, [applications, reloadControl]);

  useEffect(() => {
    const timer = window.setInterval(() => void refresh(), 20000);
    const onVisible = () => {
      if (document.visibilityState === 'visible') void refresh();
    };
    document.addEventListener('visibilitychange', onVisible);
    return () => {
      window.clearInterval(timer);
      document.removeEventListener('visibilitychange', onVisible);
    };
  }, [refresh]);

  const get = useCallback(
    (id: string | null | undefined): Playlist | null =>
      id ? (itemsRef.current ?? []).find((item) => item.id === id) ?? null : null,
    [],
  );

  const sequence = (id: string, field: Field): number => {
    const entry = fieldSeq.current[id] ?? { shuffle: 0, repeat: 0 };
    entry[field] += 1;
    fieldSeq.current[id] = entry;
    return entry[field];
  };

  const isCurrent = (id: string, field: Field, token: number): boolean =>
    (fieldSeq.current[id]?.[field] ?? 0) === token;

  const run = useCallback(
    async (
      id: string,
      field: Field,
      optimistic: Partial<Playlist>,
      patch: Record<string, unknown>,
    ) => {
      const token = sequence(id, field);
      const key = `${id}:${field}`;
      inFlight.current += 1;
      setBusy((current) => ({ ...current, [key]: field }));
      apply(id, optimistic);
      try {
        const send = (stateRevision: number) =>
          api.admin.playlists.update(id, { ...patch, state_revision: stateRevision });
        let result: PlaylistUpdateResult;
        try {
          result = await send(revisionRef.current);
        } catch (caught) {
          const stale =
            caught instanceof ApiError &&
            caught.status === 409 &&
            caught.code === 'stale_state';
          if (!stale || !isCurrent(id, field, token)) throw caught;
          const fresh = await api.admin.station.state();
          revisionRef.current = Math.max(revisionRef.current, fresh.revision);
          if (!isCurrent(id, field, token)) return;
          result = await send(revisionRef.current);
          reloadControl();
        }
        if (!isCurrent(id, field, token)) return;
        const updated = result.playlist;
        // Применяем только своё поле: второе могло измениться параллельно.
        if (field === 'shuffle') {
          apply(id, { shuffle: updated.shuffle, updatedAt: updated.updatedAt });
        } else {
          apply(id, {
            repeatMode: updated.repeatMode,
            repeatGovernedBy: updated.repeatGovernedBy,
            updatedAt: updated.updatedAt,
          });
        }
        revisionRef.current = Math.max(revisionRef.current, result.stateRevision ?? 0);
        setApplications((current) => ({
          ...current,
          [id]: { application: result.application, revision: result.stateRevision },
        }));
        if (result.application === 'pending') reloadControl();
        setError(null);
      } catch (caught) {
        if (isCurrent(id, field, token)) {
          const message = errorMessage(caught);
          setError(message);
          toast.notify('error', message);
        }
      } finally {
        inFlight.current = Math.max(0, inFlight.current - 1);
        if (isCurrent(id, field, token)) {
          setBusy((current) => {
            const next = { ...current };
            delete next[key];
            return next;
          });
        }
      }
    },
    [apply, reloadControl, toast],
  );

  const setShuffle = useCallback(
    async (id: string, value: boolean) => {
      await run(id, 'shuffle', { shuffle: value }, { shuffle: value });
    },
    [run],
  );

  const setRepeat = useCallback(
    async (id: string, value: RepeatMode) => {
      await run(id, 'repeat', { repeatMode: value }, { repeat_mode: value });
    },
    [run],
  );

  const cycleRepeat = useCallback(
    async (id: string) => {
      const current = (itemsRef.current ?? []).find((item) => item.id === id);
      if (!current) return;
      await setRepeat(id, nextRepeat(current.repeatMode));
    },
    [setRepeat],
  );

  const value = useMemo<PlaylistSettingsValue>(
    () => ({
      playlists,
      loading,
      error,
      refresh,
      get,
      busy: (id, field) => Boolean(id && busy[`${id}:${field}`]),
      application: (id) => (id ? applications[id]?.application ?? null : null),
      setShuffle,
      cycleRepeat,
      setRepeat,
    }),
    [playlists, loading, error, refresh, get, busy, applications, setShuffle, cycleRepeat, setRepeat],
  );

  return (
    <PlaylistSettingsContext.Provider value={value}>{children}</PlaylistSettingsContext.Provider>
  );
}

export function usePlaylistSettings(): PlaylistSettingsValue {
  const context = useContext(PlaylistSettingsContext);
  if (!context) throw new Error('PlaylistSettingsProvider отсутствует в дереве компонентов');
  return context;
}
