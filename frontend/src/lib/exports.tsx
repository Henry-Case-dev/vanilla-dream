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
import type { ExportJob } from './types';
import { useAuth } from './auth';

interface ExportsContextValue {
  job: ExportJob | null;
  error: string | null;
  statusOpen: boolean;
  startJob: (id: string) => void;
  cancelJob: () => Promise<void>;
  dismiss: () => void;
  openStatus: () => void;
  closeStatus: () => void;
  refresh: () => Promise<void>;
}

const ExportsContext = createContext<ExportsContextValue | null>(null);
const JOB_STORAGE_KEY = 'vd.export.job';

function readStoredJob(): string | null {
  try {
    return window.localStorage.getItem(JOB_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeJob(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(JOB_STORAGE_KEY, id);
    else window.localStorage.removeItem(JOB_STORAGE_KEY);
  } catch {
    /* без localStorage операция не переживёт перезагрузку, но продолжит работать */
  }
}

const ACTIVE_STATES: ExportJob['state'][] = ['queued', 'preparing', 'downloading'];

export function ExportProvider({ children }: { children: ReactNode }) {
  const { state: authState } = useAuth();
  const [job, setJob] = useState<ExportJob | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [statusOpen, setStatusOpen] = useState(false);
  const jobIdRef = useRef<string | null>(readStoredJob());
  const mounted = useRef(true);

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const refresh = useCallback(async () => {
    const id = jobIdRef.current;
    if (!id) return;
    try {
      const next = await api.admin.exports.get(id);
      if (!mounted.current || jobIdRef.current !== id) return;
      setJob(next);
      setError(null);
    } catch (caught) {
      if (!mounted.current) return;
      if (caught instanceof ApiError && caught.status === 404) {
        jobIdRef.current = null;
        storeJob(null);
        setJob(null);
        setStatusOpen(false);
        return;
      }
      setError(errorMessage(caught));
    }
  }, []);

  useEffect(() => {
    if (authState !== 'authenticated' || !jobIdRef.current) return;
    void refresh();
  }, [authState, refresh]);

  const active = job !== null && ACTIVE_STATES.includes(job.state);

  useEffect(() => {
    if (authState !== 'authenticated' || !active) return;
    const timer = window.setInterval(() => void refresh(), 1200);
    return () => window.clearInterval(timer);
  }, [authState, active, refresh]);

  const startJob = useCallback(
    (id: string) => {
      jobIdRef.current = id;
      storeJob(id);
      setJob({
        id,
        state: 'queued',
        processed: 0,
        total: 0,
        progress: 0,
        archive: null,
        error: null,
        expiresAt: null,
      });
      setError(null);
      setStatusOpen(false);
      void refresh();
    },
    [refresh],
  );

  const cancelJob = useCallback(async () => {
    const id = jobIdRef.current;
    if (!id) return;
    try {
      await api.admin.exports.cancel(id);
      await refresh();
    } catch (caught) {
      if (!mounted.current) return;
      setError(errorMessage(caught));
    }
  }, [refresh]);

  const dismiss = useCallback(() => {
    if (job && ACTIVE_STATES.includes(job.state)) return;
    jobIdRef.current = null;
    storeJob(null);
    setJob(null);
    setError(null);
    setStatusOpen(false);
  }, [job]);

  const value = useMemo<ExportsContextValue>(
    () => ({
      job,
      error,
      statusOpen,
      startJob,
      cancelJob,
      dismiss,
      openStatus: () => setStatusOpen(true),
      closeStatus: () => setStatusOpen(false),
      refresh,
    }),
    [job, error, statusOpen, startJob, cancelJob, dismiss, refresh],
  );

  return <ExportsContext.Provider value={value}>{children}</ExportsContext.Provider>;
}

export function useExports(): ExportsContextValue {
  const context = useContext(ExportsContext);
  if (!context) throw new Error('ExportProvider отсутствует в дереве компонентов');
  return context;
}
