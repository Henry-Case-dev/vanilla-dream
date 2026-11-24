import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  ChevronLeft,
  GripVertical,
  MoreHorizontal,
  Pencil,
  Play,
  Plus,
  Repeat,
  Repeat1,
  RotateCw,
  Shuffle,
  Trash2,
  X,
} from 'lucide-react';
import {
  Button,
  Chip,
  IconButton,
  MetaRow,
  Panel,
  TextAreaField,
  TextField,
} from '../components/ui';
import { Cover } from '../components/Cover';
import { ConfirmDialog, Modal } from '../components/Modal';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { MenuButton } from '../components/Menu';
import { useToast } from '../components/Toasts';
import { useResource } from '../lib/hooks';
import { useLive } from '../lib/live';
import { api, errorMessage } from '../lib/api';
import { formatDuration, formatLongDuration, repeatLabel, tracksLabel } from '../lib/format';
import type { RepeatMode, Track } from '../lib/types';
import { AddTracksDialog } from './AddTracksDialog';

export function PlaylistDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const { now } = useLive();
  const desired = now?.station.desired ?? 'stopped';

  const detail = useResource(
    () => (id ? api.admin.playlists.get(id) : Promise.reject(new Error('Плейлист не выбран'))),
    [id],
  );

  const playlist = detail.data?.playlist ?? null;
  const [ordered, setOrdered] = useState<Track[]>([]);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [formBusy, setFormBusy] = useState(false);
  const [savingOrder, setSavingOrder] = useState(false);
  const [dragOver, setDragOver] = useState<number | null>(null);
  const dragFrom = useRef<number | null>(null);
  const [canDrag] = useState(() => window.matchMedia('(pointer: fine)').matches);

  useEffect(() => {
    if (detail.data) setOrdered(detail.data.tracks);
  }, [detail.data]);

  const existingIds = useMemo(() => new Set(ordered.map((track) => track.id)), [ordered]);
  const totalDuration = useMemo(() => {
    if (playlist?.duration) return playlist.duration;
    return ordered.reduce((sum, track) => sum + (track.duration ?? 0), 0);
  }, [ordered, playlist]);

  const refresh = () => {
    void detail.reload(true);
  };

  const patchPlaylist = async (patch: Record<string, unknown>, message: string) => {
    if (!playlist) return;
    setActionBusy('patch');
    try {
      await api.admin.playlists.update(playlist.id, patch);
      toast.notify('success', message);
      refresh();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setActionBusy(null);
    }
  };

  const playInAir = async () => {
    if (!playlist) return;
    setActionBusy('play');
    try {
      await api.admin.playlists.play(playlist.id);
      toast.notify('success', 'Плейлист отправлен в эфир');
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setActionBusy(null);
    }
  };

  const cycleRepeat = () => {
    if (!playlist) return;
    const next: RepeatMode =
      playlist.repeatMode === 'none' ? 'all' : playlist.repeatMode === 'all' ? 'one' : 'none';
    void patchPlaylist({ repeat_mode: next }, `Режим повтора: ${repeatLabel(next).toLowerCase()}`);
  };

  const toggleShuffle = () => {
    if (!playlist) return;
    void patchPlaylist(
      { shuffle: !playlist.shuffle },
      playlist.shuffle ? 'Перемешивание выключено' : 'Перемешивание включено',
    );
  };

  const addToRotation = async () => {
    if (!playlist) return;
    setActionBusy('rotation');
    try {
      await api.admin.playlists.toRotation(playlist.id);
      toast.notify('success', 'Плейлист добавлен в ротацию');
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setActionBusy(null);
    }
  };

  const persistOrder = async (next: Track[]) => {
    if (!playlist) return;
    const previous = ordered;
    setOrdered(next);
    setSavingOrder(true);
    try {
      await api.admin.playlists.reorder(playlist.id, next.map((track) => track.id));
      toast.notify('success', 'Порядок треков сохранён');
    } catch (caught) {
      setOrdered(previous);
      toast.notify('error', errorMessage(caught));
    } finally {
      setSavingOrder(false);
    }
  };

  const handleDrop = (index: number) => {
    const from = dragFrom.current;
    dragFrom.current = null;
    setDragOver(null);
    if (from === null || from === index || savingOrder) return;
    const next = [...ordered];
    const [moved] = next.splice(from, 1);
    if (!moved) return;
    next.splice(index, 0, moved);
    void persistOrder(next);
  };

  const moveByButtons = (index: number, direction: -1 | 1) => {
    const target = index + direction;
    if (target < 0 || target >= ordered.length || savingOrder) return;
    const next = [...ordered];
    const [moved] = next.splice(index, 1);
    if (!moved) return;
    next.splice(target, 0, moved);
    void persistOrder(next);
  };

  const removeTrack = async (track: Track) => {
    if (!playlist) return;
    setRemovingId(track.id);
    try {
      await api.admin.playlists.removeTrack(playlist.id, track.id);
      setOrdered((current) => current.filter((item) => item.id !== track.id));
      toast.notify('success', 'Трек убран из плейлиста');
      refresh();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setRemovingId(null);
    }
  };

  const openEdit = () => {
    if (!playlist) return;
    setEditName(playlist.name);
    setEditDescription(playlist.description ?? '');
    setEditOpen(true);
  };

  const saveEdit = async () => {
    if (!playlist) return;
    if (!editName.trim()) {
      toast.notify('error', 'Название не может быть пустым');
      return;
    }
    setFormBusy(true);
    try {
      await api.admin.playlists.update(playlist.id, {
        name: editName.trim(),
        description: editDescription.trim() || null,
      });
      toast.notify('success', 'Плейлист обновлён');
      setEditOpen(false);
      refresh();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setFormBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!playlist) return;
    setFormBusy(true);
    try {
      await api.admin.playlists.remove(playlist.id);
      toast.notify('success', 'Плейлист удалён');
      navigate('/admin/playlists');
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
      setFormBusy(false);
    }
  };

  if (detail.loading && !detail.data) {
    return (
      <div className="page">
        <LoadingState label="Загружаем плейлист…" rows={4} />
      </div>
    );
  }

  if (detail.error && !detail.data) {
    return (
      <div className="page">
        <Link to="/admin/playlists" className="back-link">
          <ChevronLeft size={16} aria-hidden="true" /> Все плейлисты
        </Link>
        <ErrorState message={detail.error} onRetry={() => void detail.reload()} />
      </div>
    );
  }

  if (!playlist) {
    return (
      <div className="page">
        <EmptyState title="Плейлист не найден" description="Возможно, он был удалён." />
      </div>
    );
  }

  const playDisabled = desired !== 'playing' || ordered.length === 0;

  return (
    <div className="page page--playlist-detail">
      <Link to="/admin/playlists" className="back-link">
        <ChevronLeft size={16} aria-hidden="true" /> Все плейлисты
      </Link>

      <Panel className="playlist-head">
        <div className="playlist-head__cover">
          <Cover src={playlist.coverUrl} alt={`Обложка плейлиста ${playlist.name}`} size="lg" rounded="lg" />
        </div>
        <div className="playlist-head__info">
          <span className="eyebrow">Плейлист</span>
          <h1 className="playlist-head__name">{playlist.name}</h1>
          <p className="playlist-head__desc">{playlist.description || 'Без описания'}</p>
          <MetaRow>
            <span className="meta-item">{tracksLabel(ordered.length)}</span>
            <span className="meta-item">{formatLongDuration(totalDuration)}</span>
            {playlist.shuffle && <Chip tone="accent">Перемешивание</Chip>}
            <Chip tone="neutral">{repeatLabel(playlist.repeatMode)}</Chip>
          </MetaRow>

          <div className="playlist-head__actions">
            <Button
              variant="primary"
              icon={Play}
              onClick={() => void playInAir()}
              loading={actionBusy === 'play'}
              disabled={playDisabled}
              title={
                desired !== 'playing'
                  ? 'Сначала запустите эфир в нижней панели'
                  : ordered.length === 0
                    ? 'В плейлисте нет треков'
                    : 'Запустить плейлист в эфир'
              }
            >
              В эфир
            </Button>
            <Button
              variant={playlist.shuffle ? 'secondary' : 'ghost'}
              icon={Shuffle}
              onClick={toggleShuffle}
              className={playlist.shuffle ? 'is-selected' : ''}
              title="Перемешивание действует на следующий цикл плейлиста"
            >
              {playlist.shuffle ? 'Перемешивание вкл.' : 'Перемешивание'}
            </Button>
            <Button
              variant={playlist.repeatMode !== 'none' ? 'secondary' : 'ghost'}
              icon={playlist.repeatMode === 'one' ? Repeat1 : Repeat}
              onClick={cycleRepeat}
              className={playlist.repeatMode !== 'none' ? 'is-selected' : ''}
              title="Нажмите, чтобы сменить режим повтора"
            >
              {repeatLabel(playlist.repeatMode)}
            </Button>
            <Button variant="secondary" icon={Plus} onClick={() => setAddOpen(true)}>
              Добавить треки
            </Button>
            <IconButton
              icon={Pencil}
              label="Изменить название и описание"
              className="playlist-head__edit"
              onClick={openEdit}
            />
            <MenuButton
              label="Другие действия с плейлистом"
              icon={MoreHorizontal}
              items={[
                {
                  label: 'Изменить плейлист',
                  icon: Pencil,
                  onSelect: openEdit,
                },
                {
                  label: 'Добавить в ротацию',
                  icon: RotateCw,
                  onSelect: () => void addToRotation(),
                  disabled: actionBusy === 'rotation',
                },
                {
                  label: 'Удалить плейлист',
                  icon: Trash2,
                  danger: true,
                  separatorBefore: true,
                  onSelect: () => setDeleteOpen(true),
                },
              ]}
            />
          </div>
        </div>
      </Panel>

      <Panel className="playlist-tracks">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">Состав плейлиста</h2>
            <p className="panel-subtitle">
              {savingOrder ? (
                'Сохраняем порядок…'
              ) : (
                <>
                  <span className="only-desktop">
                    Перетащите трек за строку, чтобы изменить порядок воспроизведения
                  </span>
                  <span className="only-mobile">Стрелки меняют порядок треков</span>
                </>
              )}
            </p>
          </div>
          <span className="muted">{tracksLabel(ordered.length)}</span>
        </div>

        {ordered.length === 0 ? (
          <EmptyState
            icon={Plus}
            title="В плейлисте пока нет треков"
            description="Добавьте музыку из медиатеки, чтобы запустить плейлист в эфир."
            action={
              <Button variant="primary" size="sm" icon={Plus} onClick={() => setAddOpen(true)}>
                Добавить треки
              </Button>
            }
          />
        ) : (
          <div className="playlist-rows">
            <div className="playlist-row playlist-row--head" aria-hidden="true">
              <span className="playlist-row__index">№</span>
              <span />
              <span>Название</span>
              <span className="playlist-row__album">Альбом</span>
              <span className="playlist-row__duration">Время</span>
              <span />
            </div>
            {ordered.map((track, index) => (
              <div
                key={track.id}
                className={`playlist-row ${dragOver === index ? 'is-drop-target' : ''} ${
                  removingId === track.id ? 'is-removing' : ''
                }`.trim()}
                draggable={canDrag && !savingOrder}
                onDragStart={() => {
                  dragFrom.current = index;
                }}
                onDragEnd={() => {
                  dragFrom.current = null;
                  setDragOver(null);
                }}
                onDragOver={(event) => {
                  event.preventDefault();
                  if (dragFrom.current !== null && dragFrom.current !== index) setDragOver(index);
                }}
                onDrop={(event) => {
                  event.preventDefault();
                  handleDrop(index);
                }}
              >
                <span className="playlist-row__index">
                  <GripVertical size={13} className="playlist-row__grip" aria-hidden="true" />
                  {index + 1}
                </span>
                <Cover src={track.coverUrl} alt="" size="xs" />
                <div className="playlist-row__text">
                  <span className="playlist-row__title" title={track.title}>
                    {track.title}
                  </span>
                  <span className="playlist-row__artist">{track.artist ?? '—'}</span>
                </div>
                <span className="playlist-row__album" title={track.album ?? undefined}>
                  {track.album ?? '—'}
                </span>
                <span className="playlist-row__duration">{formatDuration(track.duration)}</span>
                <div className="playlist-row__actions">
                  <IconButton
                    icon={X}
                    label={`Убрать «${track.title}» из плейлиста`}
                    size="sm"
                    onClick={() => void removeTrack(track)}
                    disabled={removingId !== null}
                  />
                  <span className="playlist-row__move">
                    <IconButton
                      icon={ChevronLeft}
                      label="Переместить выше"
                      size="sm"
                      className="rotate-up"
                      disabled={index === 0 || savingOrder}
                      onClick={() => moveByButtons(index, -1)}
                    />
                    <IconButton
                      icon={ChevronLeft}
                      label="Переместить ниже"
                      size="sm"
                      className="rotate-down"
                      disabled={index === ordered.length - 1 || savingOrder}
                      onClick={() => moveByButtons(index, 1)}
                    />
                  </span>
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      <AddTracksDialog
        open={addOpen}
        playlistId={playlist.id}
        existingIds={existingIds}
        onClose={() => setAddOpen(false)}
        onAdded={refresh}
      />

      <Modal
        open={editOpen}
        onClose={() => (formBusy ? undefined : setEditOpen(false))}
        title="Изменить плейлист"
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditOpen(false)} disabled={formBusy}>
              Отмена
            </Button>
            <Button variant="primary" onClick={() => void saveEdit()} loading={formBusy}>
              Сохранить
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <TextField
            label="Название"
            value={editName}
            onChange={(event) => setEditName(event.target.value)}
          />
          <TextAreaField
            label="Описание"
            rows={3}
            value={editDescription}
            onChange={(event) => setEditDescription(event.target.value)}
          />
        </div>
      </Modal>

      <ConfirmDialog
        open={deleteOpen}
        title="Удалить плейлист?"
        description={
          <>
            Плейлист «{playlist.name}» будет удалён. Треки останутся в медиатеке, но исчезнут из
            плейлиста и будущей очереди, собранной из него.
          </>
        }
        confirmLabel="Удалить"
        danger
        busy={formBusy}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleteOpen(false)}
      />
    </div>
  );
}
