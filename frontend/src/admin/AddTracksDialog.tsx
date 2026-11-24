import { useEffect, useRef, useState } from 'react';
import { Check, Plus, Search } from 'lucide-react';
import { Button, Spinner, TextField } from '../components/ui';
import { Cover } from '../components/Cover';
import { Modal } from '../components/Modal';
import { EmptyState, ErrorState } from '../components/States';
import { useToast } from '../components/Toasts';
import { useDebounced, useResource } from '../lib/hooks';
import { api, errorMessage } from '../lib/api';
import { formatDuration } from '../lib/format';

interface AddTracksDialogProps {
  open: boolean;
  playlistId: string | null;
  existingIds: Set<string>;
  onClose: () => void;
  onAdded: () => void;
}

export function AddTracksDialog({ open, playlistId, existingIds, onClose, onAdded }: AddTracksDialogProps) {
  const toast = useToast();
  const [search, setSearch] = useState('');
  const query = useDebounced(search, 350);
  const [selected, setSelected] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);

  const tracks = useResource(
    () => (open ? api.admin.tracks.list({ q: query || undefined, limit: 50 }) : Promise.resolve(null)),
    [open, query],
  );

  const wasOpen = useRef(false);
  useEffect(() => {
    if (open && !wasOpen.current) {
      setSearch('');
      setSelected(new Set());
    }
    wasOpen.current = open;
  }, [open]);

  const toggle = (id: string) => {
    setSelected((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const submit = async () => {
    if (!playlistId || selected.size === 0) return;
    setBusy(true);
    try {
      await api.admin.playlists.addTracks(playlistId, Array.from(selected));
      toast.notify('success', `Добавлено треков: ${selected.size}`);
      onAdded();
      onClose();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const items = tracks.data?.items ?? [];

  return (
    <Modal
      open={open}
      onClose={() => (busy ? undefined : onClose())}
      title="Добавить треки в плейлист"
      size="lg"
      description="Выберите музыку из медиатеки. Треки, уже входящие в плейлист, отмечены."
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
            Добавить{selected.size > 0 ? ` (${selected.size})` : ''}
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
          autoFocus
        />
      </div>

      {tracks.loading && !tracks.data ? (
        <div className="dialog-loading">
          <Spinner /> Загружаем медиатеку…
        </div>
      ) : tracks.error ? (
        <ErrorState message={tracks.error} onRetry={() => void tracks.reload()} />
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
        <ul className="pick-list">
          {items.map((track) => {
            const inPlaylist = existingIds.has(track.id);
            const isSelected = selected.has(track.id);
            return (
              <li key={track.id}>
                <button
                  type="button"
                  className={`pick-row ${isSelected ? 'is-selected' : ''} ${
                    inPlaylist ? 'is-disabled' : ''
                  }`.trim()}
                  disabled={inPlaylist}
                  onClick={() => toggle(track.id)}
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
                  {inPlaylist && <span className="pick-row__badge">уже в плейлисте</span>}
                </button>
              </li>
            );
          })}
        </ul>
      )}
    </Modal>
  );
}
