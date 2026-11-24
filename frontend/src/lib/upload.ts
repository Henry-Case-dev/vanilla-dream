import { ApiError, getCsrfToken, handleUnauthorized } from './api';

export interface UploadHandle {
  promise: Promise<unknown>;
  abort: () => void;
}

interface UploadCallbacks {
  onProgress?: (percent: number) => void;
}

/**
 * Загрузка файла через XHR — нужен реальный прогресс отправки.
 * Cookie сессии отправляется автоматически (same-origin), CSRF — заголовком.
 */
export function uploadTrack(file: File, callbacks: UploadCallbacks = {}): UploadHandle {
  const xhr = new XMLHttpRequest();
  const promise = new Promise<unknown>((resolve, reject) => {
    const form = new FormData();
    form.append('file', file, file.name);

    xhr.open('POST', '/api/admin/tracks/upload', true);
    xhr.withCredentials = true;
    const token = getCsrfToken();
    if (token) xhr.setRequestHeader('X-CSRF-Token', token);
    xhr.setRequestHeader('Accept', 'application/json');

    xhr.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      const percent = Math.min(100, Math.round((event.loaded / event.total) * 100));
      callbacks.onProgress?.(percent);
    };

    xhr.onload = () => {
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(parseBody(xhr.responseText));
        return;
      }
      reject(buildError(xhr));
    };

    xhr.onerror = () => {
      reject(new ApiError(0, 'Нет связи с сервером во время загрузки файла', 'network'));
    };

    xhr.onabort = () => {
      reject(new ApiError(0, 'Загрузка отменена', 'aborted'));
    };

    xhr.send(form);
  });

  return {
    promise,
    abort: () => xhr.abort(),
  };
}

function parseBody(text: string): unknown {
  if (!text) return null;
  try {
    return JSON.parse(text);
  } catch {
    return null;
  }
}

function buildError(xhr: XMLHttpRequest): ApiError {
  let message = '';
  let code = `http_${xhr.status}`;
  if (xhr.status === 401) handleUnauthorized();
  const body = parseBody(xhr.responseText);
  if (body && typeof body === 'object' && 'detail' in body) {
    const detail = (body as { detail: unknown }).detail;
    if (detail && typeof detail === 'object') {
      const record = detail as { code?: unknown; message?: unknown };
      if (typeof record.message === 'string' && record.message.trim()) message = record.message;
      if (typeof record.code === 'string') code = record.code;
    } else if (typeof detail === 'string' && detail.trim()) {
      message = detail;
    }
  }

  if (!message) {
    if (xhr.status === 409 || code === 'duplicate') {
      message = 'Этот файл уже есть в медиатеке';
    } else if (xhr.status === 413) {
      message = 'Файл больше допустимого размера';
    } else if (xhr.status === 415) {
      message = 'Формат файла не поддерживается';
    } else if (xhr.status === 401) {
      message = 'Сессия истекла — войдите заново';
    } else if (xhr.status === 507) {
      message = 'Недостаточно свободного места на сервере';
    } else if (xhr.status >= 500) {
      message = 'Сервер не смог принять файл';
    } else {
      message = 'Не удалось загрузить файл';
    }
  }

  return new ApiError(xhr.status, message, code);
}

export const UPLOAD_ACCEPT = '.mp3,.flac,.ogg,.m4a,audio/mpeg,audio/flac,audio/ogg,audio/mp4';

export function isAcceptedAudioFile(file: File): boolean {
  const name = file.name.toLowerCase();
  if (/\.(mp3|flac|ogg|m4a)$/.test(name)) return true;
  return ['audio/mpeg', 'audio/flac', 'audio/ogg', 'audio/mp4', 'audio/x-m4a', 'audio/mp3'].includes(file.type);
}
