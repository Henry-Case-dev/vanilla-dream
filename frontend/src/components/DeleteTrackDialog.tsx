import { useEffect, useState } from 'react';
import { Radio } from 'lucide-react';
import { Button } from './ui';
import { Cover } from './Cover';
import { Modal } from './Modal';
import { useToast } from './Toasts';
import { useLive } from '../lib/live';
import { api, errorMessage } from '../lib/api';
import { notifyDataChanged } from '../lib/refresh';
import { pluralRu } from '../lib/format';
import type { DeleteMode, Track } from '../lib/types';

interface DeleteTrackDialogProps {
  track: Track | null;
  open: boolean;
  onClose: () => void;
  onDeleted?: () => void;
}

export function DeleteTrackDialog({ track, open, onClose, onDeleted }: DeleteTrackDialogProps) {
  const toast = useToast();
  const { now } = useLive();
  const [mode, setMode] = useState<DeleteMode>('after');
  const [busy, setBusy] = useState(false);
  const current = Boolean(track && now?.track?.id === track.id);

  useEffect(() => {
    if (open) setMode('after');
  }, [open, track?.id]);

  if (!track) return null;

  const confirm = async () => {
    setBusy(true);
    try {
      const result = await api.admin.tracks.remove(track.id, mode);
      toast.notify(
        'success',
        result.skipped
          ? 'Трек удалён, эфир переключён на следующий доступный трек'
          : result.pending
            ? 'Трек помечен на удаление: файл исчезнет после освобождения движком'
            : 'Трек удалён из медиатеки и плейлистов',
      );
      notifyDataChanged();
      onDeleted?.();
      onClose();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const playlistCount = track.playlistsCount ?? 0;

  return (
    <Modal
      open={open}
      onClose={() => (busy ? undefined : onClose())}
      title="Удалить трек из медиатеки?"
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button variant="danger" onClick={() => void confirm()} loading={busy}>
            Удалить
          </Button>
        </>
      }
    >
      <div className="delete-track">
        <Cover src={track.coverUrl} alt="" size="md" />
        <div className="delete-track__text">
          <strong>{track.title}</strong>
          <span>{track.artist ?? '—'}</span>
          <span>
            {playlistCount > 0
              ? `Входит в ${playlistCount} ${pluralRu(playlistCount, ['плейлист', 'плейлиста', 'плейлистов'])}`
              : track.playlistsCount === 0
                ? 'Не входит в пользовательские плейлисты'
                : 'Состав плейлистов уточняется'}
          </span>
        </div>
        {current && (
          <span className="delete-track__air">
            <Radio size={13} aria-hidden="true" /> Сейчас в эфире
          </span>
        )}
      </div>

      <div className="delete-mode" role="radiogroup" aria-label="Когда удалить файл">
        <button
          type="button"
          role="radio"
          aria-checked={mode === 'after'}
          className={`delete-mode__option ${mode === 'after' ? 'is-active' : ''}`.trim()}
          onClick={() => setMode('after')}
        >
          <strong>После завершения трека</strong>
          <span>Эфир не прервётся; файл удалится, когда движок его освободит.</span>
        </button>
        <button
          type="button"
          role="radio"
          aria-checked={mode === 'now'}
          className={`delete-mode__option ${mode === 'now' ? 'is-active' : ''}`.trim()}
          onClick={() => setMode('now')}
        >
          <strong>Сразу</strong>
          <span>
            {current
              ? 'Эфир переключится на следующий доступный трек, затем файл удалится.'
              : 'Файл будет удалён сразу после подтверждения.'}
          </span>
        </button>
      </div>

      <p className="delete-track__note">
        Трек исчезнет из медиатеки, «Всей медиатеки» и плейлистов. Действие необратимо.
      </p>
    </Modal>
  );
}
