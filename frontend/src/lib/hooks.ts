import { useCallback, useEffect, useRef, useState } from 'react';
import { errorMessage } from './api';

export interface Resource<T> {
  data: T | null;
  error: string | null;
  loading: boolean;
  reload: (silent?: boolean) => Promise<void>;
}

/**
 * Загрузка данных с ручным обновлением и опциональным экономным опросом.
 * При изменении deps запрос выполняется заново, устаревшие ответы отбрасываются.
 */
export function useResource<T>(
  loader: () => Promise<T>,
  deps: unknown[] = [],
  options: { pollMs?: number } = {},
): Resource<T> {
  const loaderRef = useRef(loader);
  loaderRef.current = loader;

  const [data, setData] = useState<T | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [loading, setLoading] = useState(true);
  const requestId = useRef(0);
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  // eslint-disable-next-line react-hooks/exhaustive-deps
  const run = useCallback(async (silent = false) => {
    const id = ++requestId.current;
    if (!silent) setLoading(true);
    try {
      const result = await loaderRef.current();
      if (id !== requestId.current || !mounted.current) return;
      setData(result);
      setError(null);
    } catch (caught) {
      if (id !== requestId.current || !mounted.current) return;
      setError(errorMessage(caught));
    } finally {
      if (id === requestId.current && mounted.current) setLoading(false);
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, deps);

  useEffect(() => {
    void run();
  }, [run]);

  useEffect(() => {
    if (!options.pollMs) return;
    const timer = window.setInterval(() => {
      void run(true);
    }, options.pollMs);
    return () => window.clearInterval(timer);
  }, [run, options.pollMs]);

  return { data, error, loading, reload: run };
}

/** Значение с задержкой (для поиска). */
export function useDebounced<T>(value: T, delay = 350): T {
  const [debounced, setDebounced] = useState(value);
  useEffect(() => {
    const timer = window.setTimeout(() => setDebounced(value), delay);
    return () => window.clearTimeout(timer);
  }, [value, delay]);
  return debounced;
}

/** Тик для живых часов и индикаторов. */
export function useTick(intervalMs = 15000): number {
  const [tick, setTick] = useState(0);
  useEffect(() => {
    const timer = window.setInterval(() => setTick((value) => value + 1), intervalMs);
    return () => window.clearInterval(timer);
  }, [intervalMs]);
  return tick;
}
