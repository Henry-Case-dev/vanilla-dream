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
import { uploadTrack } from './upload';
import type { UploadBatch, UploadResponse } from './types';
import { notifyDataChanged } from './refresh';
import { useAuth } from './auth';

export interface LocalUploadItem {
  key: string;
  file: File;
  status: 'queued' | 'uploading' | 'error';
  progress: number;
  error: string | null;
}

interface UploadsContextValue {
  batch: UploadBatch | null;
  batchError: string | null;
  batchId: string | null;
  localItems: LocalUploadItem[];
  uploading: boolean;
  addFiles: (files: File[]) => void;
  retry: (key: string) => void;
  refresh: () => Promise<void>;
  cancelBatch: () => Promise<void>;
  clearBatch: () => void;
}

const UploadsContext = createContext<UploadsContextValue | null>(null);
const BATCH_STORAGE_KEY = 'vd.upload.batch';

function readStoredBatch(): string | null {
  try {
    return window.localStorage.getItem(BATCH_STORAGE_KEY);
  } catch {
    return null;
  }
}

function storeBatch(id: string | null): void {
  try {
    if (id) window.localStorage.setItem(BATCH_STORAGE_KEY, id);
    else window.localStorage.removeItem(BATCH_STORAGE_KEY);
  } catch {
    /* приватный режим браузера — просто не переживём перезагрузку */
  }
}

export function UploadProvider({ children }: { children: ReactNode }) {
  const { state: authState } = useAuth();
  const [batch, setBatch] = useState<UploadBatch | null>(null);
  const [batchError, setBatchError] = useState<string | null>(null);
  const [batchId, setBatchId] = useState<string | null>(() => readStoredBatch());
  const [localItems, setLocalItems] = useState<LocalUploadItem[]>([]);
  const batchIdRef = useRef<string | null>(batchId);
  const processing = useRef(false);
  const activeUpload = useRef<ReturnType<typeof uploadTrack> | null>(null);
  const mounted = useRef(true);

  batchIdRef.current = batchId;

  useEffect(() => {
    mounted.current = true;
    return () => {
      mounted.current = false;
    };
  }, []);

  const applyBatchId = useCallback((id: string | null) => {
    batchIdRef.current = id;
    setBatchId(id);
    storeBatch(id);
  }, []);

  const clearBatch = useCallback(() => {
    applyBatchId(null);
    setBatch(null);
    setBatchError(null);
    // Ошибки, оставшиеся только на клиенте, тоже относятся к закрытой операции.
    setLocalItems((items) => items.filter((item) => item.status === 'queued' || item.status === 'uploading'));
  }, [applyBatchId]);

  const refresh = useCallback(async () => {
    const id = batchIdRef.current;
    if (!id) return;
    try {
      const next = await api.admin.uploads.get(id);
      // Пока шёл запрос, операцию могли закрыть или заменить — не воскрешаем её.
      if (!mounted.current || batchIdRef.current !== id) return;
      setBatch(next);
      setBatchError(null);
    } catch (caught) {
      if (!mounted.current) return;
      if (caught instanceof ApiError && caught.status === 404) {
        clearBatch();
        return;
      }
      setBatchError(errorMessage(caught));
    }
  }, [clearBatch]);

  // Восстанавливаем незавершённую операцию после перезагрузки страницы.
  useEffect(() => {
    if (authState !== 'authenticated' || !batchIdRef.current) return;
    void refresh();
  }, [authState, refresh]);

  const addFiles = useCallback((files: File[]) => {
    if (files.length === 0) return;
    const now = Date.now();
    setLocalItems((items) => [
      ...items,
      ...files.map((file, index) => ({
        key: `${now}-${index}-${file.name}-${Math.random().toString(36).slice(2, 8)}`,
        file,
        status: 'queued' as const,
        progress: 0,
        error: null,
      })),
    ]);
  }, []);

  const retry = useCallback((key: string) => {
    setLocalItems((items) =>
      items.map((item) =>
        item.key === key ? { ...item, status: 'queued', progress: 0, error: null } : item,
      ),
    );
  }, []);

  // Последовательная отправка: один XHR за раз, batch_id сохраняется между файлами.
  useEffect(() => {
    if (processing.current) return;
    const next = localItems.find((item) => item.status === 'queued');
    if (!next) return;
    processing.current = true;
    setLocalItems((items) =>
      items.map((item) =>
        item.key === next.key ? { ...item, status: 'uploading', progress: 0, error: null } : item,
      ),
    );

    const handle = uploadTrack(next.file, {
      batchId: batchIdRef.current,
      onProgress: (percent) => {
        setLocalItems((items) =>
          items.map((item) => (item.key === next.key ? { ...item, progress: percent } : item)),
        );
      },
    });
    activeUpload.current = handle;

    handle.promise
      .then((result: UploadResponse) => {
        applyBatchId(result.batchId);
        setLocalItems((items) => items.filter((item) => item.key !== next.key));
        if (result.status === 'added') notifyDataChanged();
        void refresh();
      })
      .catch((caught) => {
        const message = errorMessage(caught);
        const status = caught instanceof ApiError ? caught.status : 0;
        // Сервер сам записывает ошибку файла в пакет (413/422/500/507), если
        // batch_id уже существует. Не дублируем её локальной строкой.
        const recordedByServer =
          Boolean(batchIdRef.current) && [413, 422, 500, 507].includes(status);
        if (recordedByServer) {
          setLocalItems((items) => items.filter((item) => item.key !== next.key));
          void refresh();
          return;
        }
        setLocalItems((items) =>
          items.map((item) =>
            item.key === next.key
              ? {
                  ...item,
                  status: 'error',
                  progress: 0,
                  error: caught instanceof ApiError && caught.code === 'aborted' ? 'Загрузка отменена' : message,
                }
              : item,
          ),
        );
      })
      .finally(() => {
        processing.current = false;
        activeUpload.current = null;
        setLocalItems((items) => [...items]);
      });
  }, [localItems, applyBatchId, refresh]);

  // Пока сервер обрабатывает файлы или остались нерешённые совпадения,
  // держим счётчики и список актуальными без ручного обновления.
  const hasQueued = localItems.some((item) => item.status === 'queued' || item.status === 'uploading');
  const batchActive =
    batch !== null &&
    (batch.status === 'uploading' ||
      batch.status === 'processing' ||
      batch.counts.pending > 0);

  useEffect(() => {
    if (authState !== 'authenticated' || !batchId) return;
    if (!hasQueued && !batchActive) return;
    const timer = window.setInterval(() => void refresh(), 1500);
    return () => window.clearInterval(timer);
  }, [authState, batchId, hasQueued, batchActive, refresh]);

  const cancelBatch = useCallback(async () => {
    activeUpload.current?.abort();
    setLocalItems((items) =>
      items.map((item) =>
        item.status === 'queued' || item.status === 'uploading'
          ? { ...item, status: 'error', progress: 0, error: 'Загрузка отменена' }
          : item,
      ),
    );
    const id = batchIdRef.current;
    if (!id) return;
    try {
      await api.admin.uploads.cancel(id);
      await refresh();
    } catch (caught) {
      if (!mounted.current) return;
      setBatchError(errorMessage(caught));
    }
  }, [refresh]);

  const value = useMemo<UploadsContextValue>(
    () => ({
      batch,
      batchError,
      batchId,
      localItems,
      uploading: hasQueued,
      addFiles,
      retry,
      refresh,
      cancelBatch,
      clearBatch,
    }),
    [batch, batchError, batchId, localItems, hasQueued, addFiles, retry, refresh, cancelBatch, clearBatch],
  );

  return <UploadsContext.Provider value={value}>{children}</UploadsContext.Provider>;
}

export function useUploads(): UploadsContextValue {
  const context = useContext(UploadsContext);
  if (!context) throw new Error('UploadProvider отсутствует в дереве компонентов');
  return context;
}
