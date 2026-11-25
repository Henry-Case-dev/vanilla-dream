import { useEffect, useRef } from 'react';

const DATA_CHANGED_EVENT = 'vd:data-changed';

/**
 * Единая точка инвалидации данных после мутаций. Подписчики (сайдбар,
 * обзор, настройки, деталь плейлиста, live-статус) перезапрашивают свои
 * ресурсы без перезагрузки страницы.
 */
export function notifyDataChanged(): void {
  window.dispatchEvent(new Event(DATA_CHANGED_EVENT));
}

export function useDataChanged(handler: () => void, enabled = true): void {
  const handlerRef = useRef(handler);
  handlerRef.current = handler;

  useEffect(() => {
    if (!enabled) return;
    const listener = () => handlerRef.current();
    window.addEventListener(DATA_CHANGED_EVENT, listener);
    return () => window.removeEventListener(DATA_CHANGED_EVENT, listener);
  }, [enabled]);
}
