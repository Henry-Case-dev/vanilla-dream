import { useEffect, useState } from 'react';
import {
  Download,
  ListPlus,
  Music4,
  Pencil,
  Play,
  SkipForward,
  Trash2,
  X,
} from 'lucide-react';
import { Button, SelectField } from './ui';
import { MenuButton } from './Menu';
import { Modal } from './Modal';
import { useToast } from './Toasts';
import { useLive } from '../lib/live';
import { useResource } from '../lib/hooks';
import { api, errorMessage, trackDownloadUrl } from '../lib/api';
import { triggerDownload } from '../lib/download';
import { notifyDataChanged } from '../lib/refresh';
import type { Track } from '../lib/types';

interface TrackMenuProps {
  track: Track;
  playlistId?: string | null;
  systemPlaylist?: boolean;
  onEdit: (track: Track) => void;
  onDelete: (track: Track) => void;
  onRemoved?: () => void;
}

function AddToPlaylistDialog({
  track,
  open,
  onClose,
}: {
  track: Track | null;
  open: boolean;
  onClose: () => void;
}) {
  const toast = useToast();
  const playlists = useResource(() => api.admin.playlists.list(), []);
  const [target, setTarget] = useState('');
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (open) setTarget('');
  }, [open]);

  const submit = async () => {
    if (!track || !target) return;
    setBusy(true);
    try {
      const result = await api.admin.playlists.addTracks(target, [track.id]);
      if (result.added > 0) toast.notify('success', 'Трек добавлен в плейлист');
      else if (result.skipped > 0) toast.notify('info', 'Трек уже есть в этом плейлисте');
      else toast.notify('info', 'Трек недоступен для добавления');
      notifyDataChanged();
      onClose();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => (busy ? undefined : onClose())}
      title="Добавить в плейлист"
      size="sm"
      description={track ? `${track.title} · ${track.artist ?? '—'}` : undefined}
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={busy} disabled={!target}>
            Добавить
          </Button>
        </>
      }
    >
      {playlists.loading && !playlists.data ? (
        <p className="muted">Загружаем плейлисты…</p>
      ) : (
        <SelectField label="Плейлист" value={target} onChange={(event) => setTarget(event.target.value)}>
          <option value="">Выберите плейлист</option>
          {(playlists.data ?? []).map((playlist) => (
            <option key={playlist.id} value={playlist.id}>
              {playlist.name}
            </option>
          ))}
        </SelectField>
      )}
    </Modal>
  );
}

/** Единое контекстное меню трека для медиатеки и плейлистов. */
export function TrackMenu({
  track,
  playlistId = null,
  systemPlaylist = false,
  onEdit,
  onDelete,
  onRemoved,
}: TrackMenuProps) {
  const toast = useToast();
  const { now } = useLive();
  const desired = now?.station.desired ?? 'stopped';
  const [addOpen, setAddOpen] = useState(false);
  const [busy, setBusy] = useState(false);

  const action = async (request: () => Promise<void>, message: string) => {
    setBusy(true);
    try {
      await request();
      toast.notify('success', message);
      notifyDataChanged();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const removeFromPlaylist = async () => {
    if (!playlistId || systemPlaylist) return;
    setBusy(true);
    try {
      await api.admin.playlists.removeTrack(playlistId, track.id);
      toast.notify('success', 'Трек убран из плейлиста');
      notifyDataChanged();
      onRemoved?.();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <>
      <MenuButton
        label={`Действия с треком ${track.title}`}
        items={[
          {
            label: 'Воспроизвести сейчас',
            icon: Play,
            disabled: desired !== 'playing',
            hint: 'Сначала запустите эфир',
            onSelect: () =>
              void action(() => api.admin.tracks.playNow(track.id), 'Трек отправлен в эфир'),
          },
          {
            label: 'Воспроизвести следующим',
            icon: SkipForward,
            disabled: desired !== 'playing',
            hint: 'Сначала запустите эфир',
            onSelect: () =>
              void action(() => api.admin.tracks.playNext(track.id), 'Трек поставлен следующим'),
          },
          {
            label: 'Добавить в очередь',
            icon: Music4,
            onSelect: () =>
              void action(() => api.admin.tracks.enqueue(track.id), 'Трек добавлен в очередь'),
          },
          {
            label: 'Добавить в плейлист',
            icon: ListPlus,
            separatorBefore: true,
            onSelect: () => setAddOpen(true),
          },
          {
            label: 'Изменить трек',
            icon: Pencil,
            onSelect: () => onEdit(track),
          },
          {
            label: 'Скачать трек',
            icon: Download,
            onSelect: () => triggerDownload(trackDownloadUrl(track.id, 'tags')),
          },
          {
            label: 'Скачать оригинал',
            icon: Download,
            onSelect: () => triggerDownload(trackDownloadUrl(track.id, 'original')),
          },
          ...(playlistId && !systemPlaylist
            ? [
                {
                  label: 'Убрать из плейлиста',
                  icon: X,
                  separatorBefore: true,
                  disabled: busy,
                  onSelect: () => void removeFromPlaylist(),
                },
              ]
            : []),
          {
            label: 'Удалить из медиатеки',
            icon: Trash2,
            danger: true,
            separatorBefore: true,
            onSelect: () => onDelete(track),
          },
        ]}
      />
      <AddToPlaylistDialog track={track} open={addOpen} onClose={() => setAddOpen(false)} />
    </>
  );
}
