import { useEffect, useRef, useState } from 'react';
import { ImageOff, Upload } from 'lucide-react';
import { Button, TextField } from '../components/ui';
import { Cover } from '../components/Cover';
import { Modal } from '../components/Modal';
import { InlineError } from '../components/States';
import { api, errorMessage } from '../lib/api';
import { formatBytes, formatDuration } from '../lib/format';
import type { Track } from '../lib/types';

const COVER_MAX_BYTES = 8 * 1024 * 1024;
const COVER_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

interface TrackEditorDialogProps {
  open: boolean;
  track: Track | null;
  onClose: () => void;
  onSaved: (track: Track) => void;
}

export function TrackEditorDialog({ open, track, onClose, onSaved }: TrackEditorDialogProps) {
  const [title, setTitle] = useState('');
  const [artist, setArtist] = useState('');
  const [album, setAlbum] = useState('');
  const [coverFile, setCoverFile] = useState<File | null>(null);
  const [coverPreview, setCoverPreview] = useState<string | null>(null);
  const [resetCover, setResetCover] = useState(false);
  const [coverInputKey, setCoverInputKey] = useState(0);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const inputRef = useRef<HTMLInputElement | null>(null);
  const trackId = track?.id ?? null;

  useEffect(() => {
    if (!open) return;
    setTitle(track?.title ?? '');
    setArtist(track?.artist ?? '');
    setAlbum(track?.album ?? '');
    setCoverFile(null);
    setResetCover(false);
    setError(null);
  }, [open, trackId, track?.title, track?.artist, track?.album]);

  useEffect(() => {
    if (!coverFile) {
      setCoverPreview(null);
      return;
    }
    const url = URL.createObjectURL(coverFile);
    setCoverPreview(url);
    return () => URL.revokeObjectURL(url);
  }, [coverFile]);

  if (!track) return null;

  const preview = coverPreview ?? (resetCover ? null : track.coverUrl);
  const canReset = !resetCover && (Boolean(coverFile) || track.canRevertCover);

  const chooseCover = (file: File) => {
    if (!COVER_TYPES.includes(file.type)) {
      setError('Поддерживаются JPEG, PNG и WebP');
      return;
    }
    if (file.size > COVER_MAX_BYTES) {
      setError('Обложка больше 8 МБ');
      return;
    }
    setError(null);
    setResetCover(false);
    setCoverFile(file);
  };

  const revertCover = () => {
    setCoverFile(null);
    setResetCover(true);
    setCoverInputKey((key) => key + 1);
    setError(null);
  };

  const save = async () => {
    if (!title.trim()) {
      setError('Название трека не может быть пустым');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      let updated = await api.admin.tracks.update(track.id, {
        title: title.trim(),
        artist: artist.trim() || null,
        album: album.trim() || null,
      });
      if (coverFile) {
        updated = await api.admin.tracks.uploadCover(track.id, coverFile);
      } else if (resetCover) {
        updated = await api.admin.tracks.resetCover(track.id);
      }
      onSaved(updated);
    } catch (caught) {
      // Форма и введённые значения остаются на месте.
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => (busy ? undefined : onClose())}
      title="Изменить трек"
      description="Метаданные и обложка меняются только в базе и интерфейсе — исходный аудиофайл не перезаписывается."
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button variant="primary" onClick={() => void save()} loading={busy}>
            Сохранить
          </Button>
        </>
      }
    >
      <div className="track-editor">
        <div className="track-editor__cover">
          <Cover src={preview} alt="" size="lg" rounded="lg" className="track-editor__preview" />
          <span className="track-editor__cover-hint">
            {resetCover
              ? 'Будет восстановлена исходная обложка'
              : coverPreview
                ? 'Новая обложка сохранится вместе с формой'
                : 'Обложка трека'}
          </span>
          <div className="track-editor__cover-actions">
            <Button
              variant="secondary"
              size="sm"
              icon={Upload}
              onClick={() => inputRef.current?.click()}
              disabled={busy}
            >
              Изменить обложку
            </Button>
            {canReset && (
              <Button
                variant="ghost"
                size="sm"
                icon={ImageOff}
                onClick={revertCover}
                disabled={busy}
                title="Удалить пользовательскую обложку при сохранении"
              >
                Вернуть исходную
              </Button>
            )}
          </div>
          <input
            key={coverInputKey}
            ref={inputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            className="visually-hidden"
            tabIndex={-1}
            aria-label="Выбрать обложку трека"
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) chooseCover(file);
            }}
          />
        </div>

        <div className="track-editor__fields">
          <TextField
            label="Название"
            value={title}
            onChange={(event) => setTitle(event.target.value)}
            disabled={busy}
          />
          <TextField
            label="Исполнитель"
            value={artist}
            onChange={(event) => setArtist(event.target.value)}
            disabled={busy}
          />
          <TextField
            label="Альбом"
            value={album}
            onChange={(event) => setAlbum(event.target.value)}
            disabled={busy}
          />
          <p className="track-editor__meta">
            {formatDuration(track.duration)} · {formatBytes(track.size)}
            {track.sourceFilename ? ` · ${track.sourceFilename}` : ''}
          </p>
          {error && <InlineError message={error} />}
        </div>
      </div>
    </Modal>
  );
}
