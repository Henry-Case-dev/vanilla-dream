import { useEffect, useMemo, useRef, useState } from 'react';
import { Check, Plus, Search } from 'lucide-react';
import { Button, Spinner, TextField } from '../components/ui';
import { Cover } from '../components/Cover';
import { Modal } from '../components/Modal';
import { EmptyState, ErrorState, InlineError } from '../components/States';
import { useToast } from '../components/Toasts';
import { useDebounced, useResource } from '../lib/hooks';
import { api, errorMessage } from '../lib/api';
import { formatDuration } from '../lib/format';
import { notifyDataChanged } from '../lib/refresh';
import {
  scopeSelectionState,
  toggleScopeSelection,
} from '../lib/selection';
import type { AddTracksResult, Track } from '../lib/types';

const PAGE_SIZE = 50;

interface AddTracksDialogProps {
  open: boolean;
  playlistId: string | null;
  existingIds: Set<string>;
  onClose: () => void;
  onAdded: (result: AddTracksResult) => void;
}

function TriStateCheckbox({
  state,
  disabled,
  onChange,
}: {
  state: 'none' | 'some' | 'all';
  disabled: boolean;
  onChange: () => void;
}) {
  const ref = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = state === 'some';
  }, [state]);
  return (
    <input
      ref={ref}
      type="checkbox"
      checked={state === 'all'}
      disabled={disabled}
      aria-label="Выбрать все треки в текущей выборке"
      onChange={onChange}
    />
  );
}

export function AddTracksDialog({
  open,
  playlistId,
  existingIds,
  onClose,
  onAdded,
}: AddTracksDialogProps) {
  const toast = useToast();
  const [search, setSearch] = useState('');
  const query = useDebounced(search, 350);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [extraItems, setExtraItems] = useState<{ query: string; items: Track[] }>({ query: '', items: [] });
  const [loadingMore, setLoadingMore] = useState(false);

  const firstPage = useResource(
    () =>
      open && playlistId
        ? api.admin.tracks.list({ q: query || undefined, limit: PAGE_SIZE, offset: 0 })
        : Promise.resolve(null),
    [open, playlistId, query],
  );

  // Все новые id текущей выборки: нужны для tri-state «Выбрать все» и для
  // пометки треков, уже входящих в плейлист, даже за пределами первой страницы.
  const scopeIds = useResource(
    () =>
      open && playlistId
        ? api.admin.tracks.ids({ q: query || undefined, excludePlaylistId: playlistId })
        : Promise.resolve(null),
    [open, playlistId, query],
  );

  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setSearch('');
      setSelected(new Set());
      setExtraItems({ query: '', items: [] });
    }
    wasOpen.current = open;
  }, [open]);

  useEffect(() => {
    setExtraItems((current) => (current.items.length > 0 ? { query, items: [] } : current));
  }, [query]);

  const pageItems = firstPage.data?.items ?? [];
  const items = useMemo(() => {
    const combined = [...pageItems];
    if (extraItems.query === query) combined.push(...extraItems.items);
    const seen = new Set<string>();
    return combined.filter((track) => {
      if (seen.has(track.id)) return false;
      seen.add(track.id);
      return true;
    });
  }, [pageItems, extraItems, query]);

  const scope = scopeIds.data?.ids ?? [];
  const scopeSet = useMemo(() => new Set(scope), [scope]);
  const selectionState = scopeSelectionState(selected, scope);

  // Уже добавленные треки не должны оставаться в счётчике, но выбор,
  // сделанный в другом поисковом запросе, сохраняется.
  useEffect(() => {
    setSelected((current) => {
      const next = new Set<string>();
      let changed = false;
      for (const id of current) {
        if (!existingIds.has(id)) next.add(id);
        else changed = true;
      }
      return changed ? next : current;
    });
  }, [existingIds]);

  const inPlaylist = (track: Track): boolean =>
    existingIds.has(track.id) || (scopeIds.data !== null && !scopeSet.has(track.id));

  const toggle = (track: Track) => {
    if (inPlaylist(track)) return;
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(track.id)) next.delete(track.id);
      else next.add(track.id);
      return next;
    });
  };

  const toggleAll = () => {
    if (scope.length === 0) return;
    setSelected((current) => toggleScopeSelection(current, scope));
  };

  const loadMore = async () => {
    if (!playlistId || loadingMore) return;
    setLoadingMore(true);
    try {
      const next = await api.admin.tracks.list({
        q: query || undefined,
        limit: PAGE_SIZE,
        offset: items.length,
      });
      setExtraItems((current) => ({
        query,
        items: [...(current.query === query ? current.items : []), ...next.items],
      }));
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setLoadingMore(false);
    }
  };

  const submit = async () => {
    if (!playlistId || selected.size === 0) return;
    setBusy(true);
    try {
      const result = await api.admin.playlists.addTracks(playlistId, Array.from(selected));
      if (result.added > 0) {
        toast.notify('success', `Добавлено треков: ${result.added}`);
      } else if (result.skipped > 0 && result.unavailable === 0) {
        toast.notify('info', 'Все выбранные треки уже в плейлисте');
      } else {
        toast.notify(
          'info',
          `Добавлено: ${result.added} · пропущено: ${result.skipped} · недоступно: ${result.unavailable}`,
        );
      }
      notifyDataChanged();
      onAdded(result);
      onClose();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const total = firstPage.data?.total ?? 0;
  const canLoadMore = items.length < total;
  const hasSelectable = scope.length > 0;

  return (
    <Modal
      open={open}
      onClose={() => (busy ? undefined : onClose())}
      title="Добавить треки в плейлист"
      size="lg"
      description="Выберите музыку из медиатеки. Треки, уже входящие в плейлист, отмечены и не выбираются."
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button
            variant="primary"
            icon={Plus}
            onClick={() => void submit()}
            loading={busy}
            disabled={selected.size === 0}
          >
            Добавить ({selected.size})
          </Button>
        </>
      }
    >
      <div className="search-field dialog-search">
        <Search size={15} aria-hidden="true" />
        <TextField
          label="Поиск треков"
          hideLabel
          placeholder="Название, исполнитель, альбом"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      <div className="pick-bulk" role="region" aria-label="Массовый выбор треков">
        <div className="pick-bulk__left">
          <TriStateCheckbox
            state={selectionState}
            disabled={!hasSelectable || scopeIds.loading}
            onChange={toggleAll}
          />
          <div className="pick-bulk__text">
            <span className="pick-bulk__title">
              {query ? 'Выбрать все результаты поиска' : 'Выбрать всю медиатеку'}
            </span>
            <span className="pick-bulk__hint">
              {scopeIds.loading
                ? 'Считаем доступные треки…'
                : hasSelectable
                  ? `Доступно новых: ${scope.length}`
                  : 'Нет новых треков для добавления'}
            </span>
          </div>
        </div>
        <div className="pick-bulk__right">
          <span className="pick-bulk__count">Выбрано: {selected.size}</span>
          {selected.size > 0 && (
            <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
              Снять выделение
            </Button>
          )}
        </div>
      </div>
      {scopeIds.error && <InlineError message={scopeIds.error} />}

      {firstPage.loading && !firstPage.data ? (
        <div className="dialog-loading">
          <Spinner /> Загружаем медиатеку…
        </div>
      ) : firstPage.error ? (
        <ErrorState message={firstPage.error} onRetry={() => void firstPage.reload()} />
      ) : items.length === 0 ? (
        <EmptyState
          compact
          icon={Search}
          title={query ? 'Ничего не найдено' : 'Медиатека пуста'}
          description={
            query
              ? 'Измените запрос или загрузите новые треки в медиатеку.'
              : 'Сначала загрузите музыку в разделе «Медиатека».'
          }
        />
      ) : (
        <>
          <ul className="pick-list">
            {items.map((track) => {
              const already = inPlaylist(track);
              const isSelected = selected.has(track.id);
              return (
                <li key={track.id}>
                  <button
                    type="button"
                    className={`pick-row ${isSelected ? 'is-selected' : ''} ${
                      already ? 'is-disabled' : ''
                    }`.trim()}
                    disabled={already}
                    onClick={() => toggle(track)}
                  >
                    <span className={`pick-row__check ${isSelected ? 'is-on' : ''}`.trim()} aria-hidden="true">
                      {isSelected && <Check size={12} />}
                    </span>
                    <Cover src={track.coverUrl} alt="" size="xs" />
                    <span className="pick-row__text">
                      <span className="pick-row__title">{track.title}</span>
                      <span className="pick-row__sub">{track.artist ?? '—'}</span>
                    </span>
                    <span className="pick-row__meta">{formatDuration(track.duration)}</span>
                    {already && <span className="pick-row__badge">Уже в плейлисте</span>}
                  </button>
                </li>
              );
            })}
          </ul>
          <div className="pick-footer">
            <span className="muted">
              Показаны {items.length} из {total}
            </span>
            {canLoadMore && (
              <Button
                variant="secondary"
                size="sm"
                onClick={() => void loadMore()}
                loading={loadingMore}
              >
                Показать ещё
              </Button>
            )}
          </div>
        </>
      )}
    </Modal>
  );
}
