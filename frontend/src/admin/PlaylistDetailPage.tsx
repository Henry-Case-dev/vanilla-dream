import { useEffect, useMemo, useRef, useState } from 'react';
import { Link, useNavigate, useParams } from 'react-router-dom';
import {
  ChevronLeft,
  Download,
  GripVertical,
  ImageOff,
  MoreHorizontal,
  Pencil,
  Play,
  Plus,
  Repeat,
  Repeat1,
  RotateCw,
  Shuffle,
  Trash2,
  Upload,
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
import { DuplicateBadge } from '../components/DuplicateBadge';
import { ManualOverrideBanner } from '../components/ManualOverrideBanner';
import { useToast } from '../components/Toasts';
import { useResource } from '../lib/hooks';
import { useLive } from '../lib/live';
import { useDataChanged, notifyDataChanged } from '../lib/refresh';
import { api, errorMessage, trackDownloadUrl } from '../lib/api';
import { triggerDownload } from '../lib/download';
import { formatDuration, formatLongDuration, repeatLabel, tracksLabel } from '../lib/format';
import type { AddTracksResult, ExportComposition, RepeatMode, Track } from '../lib/types';
import { AddTracksDialog } from './AddTracksDialog';
import { TrackEditorDialog } from './TrackEditorDialog';
import { ExportDialog } from './ExportDialog';

const COVER_MAX_BYTES = 8 * 1024 * 1024;
const COVER_TYPES = ['image/jpeg', 'image/png', 'image/webp'];

export function PlaylistDetailPage() {
  const { id } = useParams<{ id: string }>();
  const navigate = useNavigate();
  const toast = useToast();
  const { reload: reloadLive } = useLive();

  const detail = useResource(
    () => (id ? api.admin.playlists.get(id) : Promise.reject(new Error('Плейлист не выбран'))),
    [id],
  );
  useDataChanged(() => void detail.reload(true));

  const playlist = detail.data?.playlist ?? null;
  const [ordered, setOrdered] = useState<Track[]>([]);
  const [actionBusy, setActionBusy] = useState<string | null>(null);
  const [removingId, setRemovingId] = useState<string | null>(null);
  const [addOpen, setAddOpen] = useState(false);
  const [editOpen, setEditOpen] = useState(false);
  const [editTrack, setEditTrack] = useState<Track | null>(null);
  const [exportComposition, setExportComposition] = useState<ExportComposition | null>(null);
  const [deleteOpen, setDeleteOpen] = useState(false);
  const [editName, setEditName] = useState('');
  const [editDescription, setEditDescription] = useState('');
  const [formBusy, setFormBusy] = useState(false);
  const [coverBusy, setCoverBusy] = useState(false);
  const [savingOrder, setSavingOrder] = useState(false);
  const [dragOver, setDragOver] = useState<number | null>(null);
  const dragFrom = useRef<number | null>(null);
  const coverInputRef = useRef<HTMLInputElement | null>(null);
  const [canDrag] = useState(() => window.matchMedia('(pointer: fine)').matches);

  useEffect(() => {
    if (detail.data) setOrdered(detail.data.tracks);
  }, [detail.data]);

  const existingIds = useMemo(() => new Set(ordered.map((track) => track.id)), [ordered]);
  const totalDuration = useMemo(() => {
    if (playlist) return playlist.duration;
    return ordered.reduce((sum, track) => sum + (track.duration ?? 0), 0);
  }, [ordered, playlist]);

  const refresh = () => {
    void detail.reload(true);
    notifyDataChanged();
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
    if (!playlist || playlist.trackCount === 0) return;
    setActionBusy('play');
    try {
      await api.admin.playlists.play(playlist.id, 'now');
      toast.notify('success', 'Плейлист запущен в эфир');
      reloadLive();
      refresh();
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
      notifyDataChanged();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setActionBusy(null);
    }
  };

  const persistOrder = async (next: Track[]) => {
    if (!playlist || playlist.system) return;
    if ((detail.data?.tracksTotal ?? ordered.length) > ordered.length) return;
    const previous = ordered;
    setOrdered(next);
    setSavingOrder(true);
    try {
      await api.admin.playlists.reorder(playlist.id, next.map((track) => track.id));
      toast.notify('success', 'Порядок треков сохранён');
      refresh();
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
    if (!playlist || playlist.system) return;
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

  const onTracksAdded = (result: AddTracksResult) => {
    if (result.added > 0) {
      setOrdered((current) => [...current]);
      void detail.reload(true);
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
    if (playlist.system) {
      setEditOpen(false);
      return;
    }
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
    if (!playlist || playlist.system) return;
    setFormBusy(true);
    try {
      await api.admin.playlists.remove(playlist.id);
      toast.notify('success', 'Плейлист удалён');
      notifyDataChanged();
      navigate('/admin/playlists');
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
      setFormBusy(false);
    }
  };

  const uploadCover = async (file: File) => {
    if (!playlist) return;
    if (!COVER_TYPES.includes(file.type)) {
      toast.notify('error', 'Поддерживаются JPEG, PNG и WebP');
      if (coverInputRef.current) coverInputRef.current.value = '';
      return;
    }
    if (file.size > COVER_MAX_BYTES) {
      toast.notify('error', 'Обложка больше 8 МБ');
      if (coverInputRef.current) coverInputRef.current.value = '';
      return;
    }
    setCoverBusy(true);
    try {
      await api.admin.playlists.uploadCover(playlist.id, file);
      toast.notify('success', 'Обложка обновлена');
      await detail.reload(true);
      notifyDataChanged();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setCoverBusy(false);
      if (coverInputRef.current) coverInputRef.current.value = '';
    }
  };

  const resetCover = async () => {
    if (!playlist) return;
    setCoverBusy(true);
    try {
      await api.admin.playlists.resetCover(playlist.id);
      toast.notify('success', 'Вернули автоматическую обложку');
      await detail.reload(true);
      notifyDataChanged();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setCoverBusy(false);
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

  const isSystem = playlist.system;
  const isEmpty = playlist.trackCount === 0;
  const playDisabled = isEmpty;
  const tracksTotal = detail.data?.tracksTotal ?? ordered.length;
  const limited = tracksTotal > ordered.length;
  const canReorder = !isSystem && !savingOrder && !limited;

  const openPlaylistExport = () => {
    const sizeKnown = ordered.every((track) => track.size !== null);
    setExportComposition({
      scope: isSystem ? 'library' : 'playlist',
      playlistId: isSystem ? undefined : playlist.id,
      title: playlist.name,
      coverUrl: playlist.coverUrl,
      count: playlist.trackCount,
      size: sizeKnown ? ordered.reduce((sum, track) => sum + (track.size ?? 0), 0) : null,
      partialSize: limited,
    });
  };

  return (
    <div className="page page--playlist-detail">
      <Link to="/admin/playlists" className="back-link">
        <ChevronLeft size={16} aria-hidden="true" /> Все плейлисты
      </Link>

      <ManualOverrideBanner />

      <Panel className="playlist-head">
        <div className="playlist-head__cover">
          <button
            type="button"
            className="playlist-head__cover-button"
            onClick={openEdit}
            title="Изменить обложку плейлиста"
            aria-label="Изменить обложку плейлиста"
          >
            <Cover
              src={playlist.coverUrl}
              alt={`Обложка плейлиста ${playlist.name}`}
              size="lg"
              rounded="lg"
            />
          </button>
        </div>
        <div className="playlist-head__info">
          <div className="playlist-head__eyebrow">
            <span className="eyebrow">Плейлист</span>
            {isSystem && <span className="playlist-system-label">Системный плейлист</span>}
          </div>
          <h1 className="playlist-head__name">{playlist.name}</h1>
          <p className="playlist-head__desc">{playlist.description || 'Без описания'}</p>
          <MetaRow>
            <span className="meta-item">{tracksLabel(playlist.trackCount)}</span>
            <span className="meta-item">{formatLongDuration(totalDuration)}</span>
            {playlist.shuffle && <Chip tone="accent">Перемешивание</Chip>}
            <Chip tone="neutral">{repeatLabel(playlist.repeatMode)}</Chip>
            {playlist.pendingDeleteCount ? (
              <Chip tone="warning">
                {playlist.pendingDeleteCount} ждут удаления после эфира
              </Chip>
            ) : null}
          </MetaRow>

          <div className="playlist-head__actions">
            <Button
              variant="primary"
              icon={Play}
              onClick={() => void playInAir()}
              loading={actionBusy === 'play'}
              disabled={playDisabled}
              title={isEmpty ? 'В плейлисте нет доступных треков' : 'Запустить плейлист в эфир'}
            >
              В эфир
            </Button>
            <Button
              variant="secondary"
              icon={Download}
              onClick={openPlaylistExport}
              disabled={isEmpty}
              title={isEmpty ? 'В плейлисте пока нет треков' : 'Скачать плейлист'}
            >
              Скачать
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
            {!isSystem && (
              <Button variant="secondary" icon={Plus} onClick={() => setAddOpen(true)}>
                Добавить треки
              </Button>
            )}
            <IconButton
              icon={Pencil}
              label={isSystem ? 'Изменить обложку' : 'Изменить плейлист'}
              className="playlist-head__edit"
              onClick={openEdit}
            />
            <MenuButton
              label="Другие действия с плейлистом"
              icon={MoreHorizontal}
              items={[
                ...(isSystem
                  ? []
                  : [
                      {
                        label: 'Изменить плейлист',
                        icon: Pencil,
                        onSelect: openEdit,
                      },
                    ]),
                {
                  label: 'Добавить в ротацию',
                  icon: RotateCw,
                  onSelect: () => void addToRotation(),
                  disabled: actionBusy === 'rotation',
                },
                ...(isSystem
                  ? []
                  : [
                      {
                        label: 'Удалить плейлист',
                        icon: Trash2,
                        danger: true,
                        separatorBefore: true,
                        onSelect: () => setDeleteOpen(true),
                      },
                    ]),
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
              {isSystem
                ? 'Состав меняется автоматически вместе с медиатекой'
                : savingOrder
                  ? 'Сохраняем порядок…'
                  : limited
                    ? 'Полный состав не загружен — изменение порядка недоступно'
                    : (
                      <>
                        <span className="only-desktop">
                          Перетащите трек за строку, чтобы изменить порядок воспроизведения
                        </span>
                        <span className="only-mobile">Стрелки меняют порядок треков</span>
                      </>
                    )}
            </p>
          </div>
          <span className="muted">{tracksLabel(playlist.trackCount)}</span>
        </div>

        {limited && (
          <p className="panel-note">
            Показаны первые {ordered.length} из {tracksTotal} треков. Полный состав доступен
            движку и учитывается в длительности.
          </p>
        )}

        {ordered.length === 0 ? (
          isSystem ? (
            <EmptyState
              icon={Upload}
              title="Медиатека пуста"
              description="Загрузите музыку — системный плейлист «Вся медиатека» наполнится автоматически."
              action={
                <Link className="btn btn--primary btn--sm" to="/admin/library?upload=1">
                  <Upload size={14} aria-hidden="true" />
                  <span className="btn__label">Загрузить музыку</span>
                </Link>
              }
            />
          ) : (
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
          )
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
                } ${isSystem ? 'playlist-row--system' : ''}`.trim()}
                draggable={canDrag && !savingOrder && !isSystem && !limited}
                onDragStart={() => {
                  if (!canReorder) return;
                  dragFrom.current = index;
                }}
                onDragEnd={() => {
                  dragFrom.current = null;
                  setDragOver(null);
                }}
                onDragOver={(event) => {
                  if (!canReorder) return;
                  event.preventDefault();
                  if (dragFrom.current !== null && dragFrom.current !== index) setDragOver(index);
                }}
                onDrop={(event) => {
                  if (!canReorder) return;
                  event.preventDefault();
                  handleDrop(index);
                }}
              >
                <span className="playlist-row__index">
                  {!isSystem && <GripVertical size={13} className="playlist-row__grip" aria-hidden="true" />}
                  {index + 1}
                </span>
                <Cover src={track.coverUrl} alt="" size="xs" />
                <div className="playlist-row__text">
                  <span className="playlist-row__title-row">
                    <span className="playlist-row__title" title={track.title}>
                      {track.title}
                    </span>
                    {track.isDuplicate && <DuplicateBadge kind={track.duplicateKind} />}
                  </span>
                  <span className="playlist-row__artist">{track.artist ?? '—'}</span>
                </div>
                <span className="playlist-row__album" title={track.album ?? undefined}>
                  {track.album ?? '—'}
                </span>
                <span className="playlist-row__duration">{formatDuration(track.duration)}</span>
                <div className="playlist-row__actions">
                  <MenuButton
                    label={`Действия с треком ${track.title}`}
                    items={[
                      {
                        label: 'Изменить трек',
                        icon: Pencil,
                        onSelect: () => setEditTrack(track),
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
                      ...(!isSystem
                        ? [
                            {
                              label: 'Убрать из плейлиста',
                              icon: X,
                              danger: true,
                              separatorBefore: true,
                              onSelect: () => void removeTrack(track),
                            },
                          ]
                        : []),
                    ]}
                  />
                  {!isSystem && (
                    <>
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
                          disabled={index === 0 || !canReorder}
                          onClick={() => moveByButtons(index, -1)}
                        />
                        <IconButton
                          icon={ChevronLeft}
                          label="Переместить ниже"
                          size="sm"
                          className="rotate-down"
                          disabled={index === ordered.length - 1 || !canReorder}
                          onClick={() => moveByButtons(index, 1)}
                        />
                      </span>
                    </>
                  )}
                </div>
              </div>
            ))}
          </div>
        )}
      </Panel>

      {!isSystem && (
        <AddTracksDialog
          open={addOpen}
          playlistId={playlist.id}
          existingIds={existingIds}
          onClose={() => setAddOpen(false)}
          onAdded={onTracksAdded}
        />
      )}

      <Modal
        open={editOpen}
        onClose={() => (formBusy || coverBusy ? undefined : setEditOpen(false))}
        title={isSystem ? 'Обложка системного плейлиста' : 'Изменить плейлист'}
        description={
          isSystem
            ? 'Название и состав «Всей медиатеки» фиксированы, обложку можно сменить.'
            : 'Название, описание и обложку можно изменить.'
        }
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditOpen(false)} disabled={formBusy || coverBusy}>
              {isSystem ? 'Закрыть' : 'Отмена'}
            </Button>
            {!isSystem && (
              <Button variant="primary" onClick={() => void saveEdit()} loading={formBusy}>
                Сохранить
              </Button>
            )}
          </>
        }
      >
        <div className="form-grid">
          <div className="cover-editor">
            <Cover
              src={playlist.coverUrl}
              alt={`Обложка плейлиста ${playlist.name}`}
              size="lg"
              rounded="lg"
              className="cover-editor__preview"
            />
            <div className="cover-editor__body">
              <span className="cover-editor__title">Обложка плейлиста</span>
              <span className="cover-editor__hint">
                JPEG, PNG или WebP до 8 МБ. Квадратная обложка обрезается автоматически.
              </span>
              <div className="cover-editor__actions">
                <Button
                  variant="secondary"
                  size="sm"
                  icon={Upload}
                  onClick={() => coverInputRef.current?.click()}
                  loading={coverBusy}
                >
                  {playlist.coverKind === 'custom' ? 'Заменить обложку' : 'Загрузить обложку'}
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  icon={ImageOff}
                  onClick={() => void resetCover()}
                  disabled={coverBusy || playlist.coverKind !== 'custom'}
                  title={
                    playlist.coverKind === 'custom'
                      ? 'Удалить пользовательскую обложку'
                      : 'Сейчас используется автоматическая обложка'
                  }
                >
                  Вернуть автоматическую обложку
                </Button>
              </div>
            </div>
          </div>
          <input
            ref={coverInputRef}
            type="file"
            accept="image/jpeg,image/png,image/webp"
            className="visually-hidden"
            tabIndex={-1}
            onChange={(event) => {
              const file = event.target.files?.[0];
              if (file) void uploadCover(file);
            }}
          />
          {!isSystem && (
            <>
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
            </>
          )}
        </div>
      </Modal>

      {!isSystem && (
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
      )}

      <TrackEditorDialog
        open={editTrack !== null}
        track={editTrack}
        onClose={() => setEditTrack(null)}
        onSaved={() => {
          toast.notify('success', 'Трек сохранён');
          setEditTrack(null);
          void detail.reload(true);
          notifyDataChanged();
        }}
      />

      <ExportDialog
        open={exportComposition !== null}
        composition={exportComposition}
        onClose={() => setExportComposition(null)}
      />
    </div>
  );
}
