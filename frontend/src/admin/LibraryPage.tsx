import { useEffect, useRef, useState } from 'react';
import type { DragEvent } from 'react';
import {
  ArrowDown,
  ArrowUp,
  ArrowUpDown,
  Check,
  ListPlus,
  Music4,
  Pause,
  Play,
  RefreshCw,
  Search,
  Trash2,
  Upload,
  X,
} from 'lucide-react';
import type { Track, TrackSortField } from '../lib/types';
import {
  Button,
  Equalizer,
  IconButton,
  PageHeader,
  Panel,
  ProgressBar,
  SelectField,
  Spinner,
  TextField,
} from '../components/ui';
import { Cover } from '../components/Cover';
import { ConfirmDialog, Modal } from '../components/Modal';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { MenuButton } from '../components/Menu';
import { useToast } from '../components/Toasts';
import { useDebounced, useResource } from '../lib/hooks';
import { useLive } from '../lib/live';
import { useAudio } from '../lib/audio';
import { api, ApiError, errorMessage } from '../lib/api';
import { formatBytes, formatCount, formatDuration, pluralRu, tracksLabel } from '../lib/format';
import { isAcceptedAudioFile, UPLOAD_ACCEPT, uploadTrack } from '../lib/upload';
import type { UploadHandle } from '../lib/upload';

const PAGE_SIZE = 50;

type UploadStatus = 'queued' | 'uploading' | 'done' | 'error' | 'duplicate';

interface UploadItem {
  id: string;
  file: File;
  status: UploadStatus;
  progress: number;
  error: string | null;
}

interface EditForm {
  title: string;
  artist: string;
  album: string;
  genre: string;
  year: string;
}

const emptyEditForm: EditForm = { title: '', artist: '', album: '', genre: '', year: '' };

function HeaderCheckbox({
  checked,
  indeterminate,
  onChange,
}: {
  checked: boolean;
  indeterminate: boolean;
  onChange: (checked: boolean) => void;
}) {
  const ref = useRef<HTMLInputElement | null>(null);
  useEffect(() => {
    if (ref.current) ref.current.indeterminate = indeterminate;
  }, [indeterminate]);
  return (
    <input
      ref={ref}
      type="checkbox"
      checked={checked}
      aria-label="Выбрать все треки на странице"
      onChange={(event) => onChange(event.target.checked)}
    />
  );
}

export function LibraryPage() {
  const toast = useToast();
  const { now } = useLive();
  const audio = useAudio();
  const desired = now?.station.desired ?? 'stopped';

  const [searchInput, setSearchInput] = useState('');
  const query = useDebounced(searchInput, 350);
  const [sort, setSort] = useState<TrackSortField>('created');
  const [order, setOrder] = useState<'asc' | 'desc'>('desc');
  const [pageIndex, setPageIndex] = useState(0);

  const tracks = useResource(
    () =>
      api.admin.tracks.list({
        q: query || undefined,
        sort,
        order,
        limit: PAGE_SIZE,
        offset: pageIndex * PAGE_SIZE,
      }),
    [query, sort, order, pageIndex],
  );
  const reloadTracks = useRef(tracks.reload);
  reloadTracks.current = tracks.reload;

  const settings = useResource(() => api.admin.settings.get(), []);
  const playlists = useResource(() => api.admin.playlists.list(), []);

  const [selected, setSelected] = useState<Set<string>>(new Set());
  useEffect(() => {
    setSelected(new Set());
  }, [query, sort, order, pageIndex]);
  useEffect(() => {
    setPageIndex(0);
  }, [query, sort, order]);

  const [editTrack, setEditTrack] = useState<Track | null>(null);
  const [editForm, setEditForm] = useState<EditForm>(emptyEditForm);
  const [editBusy, setEditBusy] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<Track | null>(null);
  const [deleteBusy, setDeleteBusy] = useState(false);
  const [bulkDeleteOpen, setBulkDeleteOpen] = useState(false);
  const [bulkBusy, setBulkBusy] = useState(false);
  const [addToPlaylistOpen, setAddToPlaylistOpen] = useState(false);
  const [addTargetPlaylist, setAddTargetPlaylist] = useState('');

  const [previewTrack, setPreviewTrack] = useState<Track | null>(null);
  const [previewPlaying, setPreviewPlaying] = useState(false);
  const previewRef = useRef<HTMLAudioElement | null>(null);

  const [uploadOpen, setUploadOpen] = useState(false);
  const [uploadItems, setUploadItems] = useState<UploadItem[]>([]);
  const [uploadRunning, setUploadRunning] = useState(false);
  const [dragActive, setDragActive] = useState(false);
  const dragCounter = useRef(0);
  const processing = useRef(false);
  const activeUpload = useRef<UploadHandle | null>(null);

  const maxUploadMb = settings.data?.maxUploadMb ?? 300;

  useEffect(() => {
    if (editTrack) {
      setEditForm({
        title: editTrack.title,
        artist: editTrack.artist ?? '',
        album: editTrack.album ?? '',
        genre: editTrack.genre ?? '',
        year: editTrack.year ? String(editTrack.year) : '',
      });
    }
  }, [editTrack]);

  /* ------------------------------ загрузка ------------------------------ */

  const addFiles = (files: File[]) => {
    if (files.length === 0) return;
    const accepted: File[] = [];
    let rejected = 0;
    const tooBig: UploadItem[] = [];
    files.forEach((file) => {
      if (!isAcceptedAudioFile(file)) {
        rejected += 1;
        return;
      }
      if (file.size > maxUploadMb * 1024 * 1024) {
        tooBig.push({
          id: `${Date.now()}-${file.name}-${Math.random().toString(36).slice(2, 8)}`,
          file,
          status: 'error',
          progress: 0,
          error: `Файл больше ${maxUploadMb} МБ`,
        });
        return;
      }
      accepted.push(file);
    });
    if (rejected > 0) {
      toast.notify('error', `Пропущено файлов: ${rejected}. Поддерживаются MP3, FLAC, OGG, M4A`);
    }
    const next: UploadItem[] = accepted.map((file, index) => ({
      id: `${Date.now()}-${index}-${file.name}-${Math.random().toString(36).slice(2, 8)}`,
      file,
      status: 'queued',
      progress: 0,
      error: null,
    }));
    setUploadItems((items) => [...items, ...next, ...tooBig]);
    setUploadOpen(true);
  };

  useEffect(() => {
    if (processing.current) return;
    const next = uploadItems.find((item) => item.status === 'queued');
    if (!next) {
      setUploadRunning(false);
      return;
    }
    processing.current = true;
    setUploadRunning(true);
    setUploadItems((items) =>
      items.map((item) =>
        item.id === next.id ? { ...item, status: 'uploading', progress: 0, error: null } : item,
      ),
    );

    const handle = uploadTrack(next.file, {
      onProgress: (percent) => {
        setUploadItems((items) =>
          items.map((item) => (item.id === next.id ? { ...item, progress: percent } : item)),
        );
      },
    });
    activeUpload.current = handle;

    handle.promise
      .then(() => {
        setUploadItems((items) =>
          items.map((item) =>
            item.id === next.id ? { ...item, status: 'done', progress: 100 } : item,
          ),
        );
        void reloadTracks.current(true);
      })
      .catch((caught) => {
        const aborted = caught instanceof ApiError && caught.code === 'aborted';
        const duplicate =
          caught instanceof ApiError && (caught.code === 'duplicate' || caught.status === 409);
        setUploadItems((items) =>
          items.map((item) =>
            item.id === next.id
              ? {
                  ...item,
                  status: aborted ? 'error' : duplicate ? 'duplicate' : 'error',
                  error: aborted ? 'Загрузка отменена' : errorMessage(caught),
                }
              : item,
          ),
        );
      })
      .finally(() => {
        processing.current = false;
        activeUpload.current = null;
        setUploadItems((items) => [...items]);
      });
  }, [uploadItems]);

  const cancelUploads = () => {
    activeUpload.current?.abort();
    setUploadItems((items) =>
      items.map((item) =>
        item.status === 'queued' || item.status === 'uploading'
          ? { ...item, status: 'error', error: 'Загрузка отменена' }
          : item,
      ),
    );
  };

  const retryFailed = () => {
    setUploadItems((items) =>
      items.map((item) =>
        item.status === 'error' && item.error !== 'Файл больше допустимого размера'
          ? { ...item, status: 'queued', progress: 0, error: null }
          : item,
      ),
    );
  };

  const closeUpload = () => {
    if (uploadRunning) {
      toast.notify('info', 'Дождитесь завершения загрузки или отмените её');
      return;
    }
    setUploadOpen(false);
    setUploadItems((items) => items.filter((item) => item.status !== 'done'));
  };

  const uploadStats = uploadItems.reduce(
    (acc, item) => {
      acc[item.status] += 1;
      return acc;
    },
    { queued: 0, uploading: 0, done: 0, error: 0, duplicate: 0 } as Record<UploadStatus, number>,
  );

  /* ---------------------------- drag-and-drop --------------------------- */

  const onDragEnter = (event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    dragCounter.current += 1;
    setDragActive(true);
  };

  const onDragOver = (event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
  };

  const onDragLeave = (event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    dragCounter.current = Math.max(0, dragCounter.current - 1);
    if (dragCounter.current === 0) setDragActive(false);
  };

  const onDrop = (event: DragEvent<HTMLDivElement>) => {
    if (!Array.from(event.dataTransfer.types).includes('Files')) return;
    event.preventDefault();
    dragCounter.current = 0;
    setDragActive(false);
    addFiles(Array.from(event.dataTransfer.files));
  };

  /* ------------------------------ действия ------------------------------ */

  const toggleSelected = (id: string, checked: boolean) => {
    setSelected((current) => {
      const next = new Set(current);
      if (checked) next.add(id);
      else next.delete(id);
      return next;
    });
  };

  const pageItems = tracks.data?.items ?? [];
  const allSelected = pageItems.length > 0 && pageItems.every((track) => selected.has(track.id));
  const someSelected = pageItems.some((track) => selected.has(track.id));
  const selectedIds = Array.from(selected);

  const saveMetadata = async () => {
    if (!editTrack) return;
    if (!editForm.title.trim()) {
      toast.notify('error', 'Название трека не может быть пустым');
      return;
    }
    let year: number | null = null;
    if (editForm.year.trim()) {
      const parsed = Number.parseInt(editForm.year.trim(), 10);
      if (!Number.isFinite(parsed) || parsed < 1900 || parsed > 2100) {
        toast.notify('error', 'Год должен быть числом от 1900 до 2100');
        return;
      }
      year = parsed;
    }
    setEditBusy(true);
    try {
      await api.admin.tracks.update(editTrack.id, {
        title: editForm.title.trim(),
        artist: editForm.artist.trim() || null,
        album: editForm.album.trim() || null,
        genre: editForm.genre.trim() || null,
        year,
      });
      toast.notify('success', 'Метаданные сохранены');
      setEditTrack(null);
      void tracks.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setEditBusy(false);
    }
  };

  const confirmDelete = async () => {
    if (!deleteTarget) return;
    setDeleteBusy(true);
    try {
      const result = await api.admin.tracks.remove(deleteTarget.id);
      toast.notify(
        'success',
        result.pending
          ? 'Трек удалится после смены трека в эфире'
          : 'Трек удалён из медиатеки и плейлистов',
      );
      setDeleteTarget(null);
      void tracks.reload(true);
      void playlists.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setDeleteBusy(false);
    }
  };

  const confirmBulkDelete = async () => {
    if (selectedIds.length === 0) return;
    setBulkBusy(true);
    try {
      await api.admin.tracks.bulkDelete(selectedIds);
      toast.notify('success', `Удалено треков: ${selectedIds.length}`);
      setBulkDeleteOpen(false);
      setSelected(new Set());
      void tracks.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBulkBusy(false);
    }
  };

  const addSelectionToPlaylist = async () => {
    if (!addTargetPlaylist || selectedIds.length === 0) return;
    setBulkBusy(true);
    try {
      await api.admin.playlists.addTracks(addTargetPlaylist, selectedIds);
      toast.notify('success', `Добавлено треков: ${selectedIds.length}`);
      setAddToPlaylistOpen(false);
      setSelected(new Set());
      void playlists.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBulkBusy(false);
    }
  };

  const togglePreview = (track: Track) => {
    const element = previewRef.current;
    if (!element) return;
    if (previewTrack?.id === track.id) {
      if (previewPlaying) element.pause();
      else void element.play().catch(() => toast.notify('error', 'Не удалось воспроизвести трек'));
      return;
    }
    audio.stop();
    setPreviewTrack(track);
    element.src = `/api/admin/tracks/${encodeURIComponent(track.id)}/stream`;
    element.load();
    void element.play().catch(() => {
      toast.notify('error', 'Не удалось воспроизвести трек');
      setPreviewTrack(null);
    });
  };

  const stopPreview = () => {
    const element = previewRef.current;
    if (element) {
      element.pause();
      element.removeAttribute('src');
      element.load();
    }
    setPreviewTrack(null);
    setPreviewPlaying(false);
  };

  const trackAction = async (action: () => Promise<void>, message: string) => {
    try {
      await action();
      toast.notify('success', message);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    }
  };

  const sortHeader = (field: TrackSortField, label: string) => {
    const active = sort === field;
    const Icon = !active ? ArrowUpDown : order === 'asc' ? ArrowUp : ArrowDown;
    return (
      <button
        type="button"
        className={`th-sort ${active ? 'is-active' : ''}`.trim()}
        onClick={() => {
          if (active) setOrder(order === 'asc' ? 'desc' : 'asc');
          else {
            setSort(field);
            setOrder('asc');
          }
        }}
        aria-sort={active ? (order === 'asc' ? 'ascending' : 'descending') : 'none'}
      >
        {label}
        <Icon size={12} aria-hidden="true" />
      </button>
    );
  };

  const total = tracks.data?.total ?? 0;
  const shownFrom = total === 0 ? 0 : pageIndex * PAGE_SIZE + 1;
  const shownTo = Math.min(pageIndex * PAGE_SIZE + pageItems.length, total);
  const hasNext = pageIndex * PAGE_SIZE + PAGE_SIZE < total;

  return (
    <div
      className="page page--library"
      onDragEnter={onDragEnter}
      onDragOver={onDragOver}
      onDragLeave={onDragLeave}
      onDrop={onDrop}
    >
      <PageHeader
        title="Медиатека"
        subtitle={
          tracks.data
            ? `${tracksLabel(tracks.data.library.count)} · ${formatBytes(tracks.data.library.bytes)}`
            : 'Музыка станции'
        }
        actions={
          <>
            <IconButton
              icon={RefreshCw}
              label="Обновить список"
              title="Обновить список"
              onClick={() => void tracks.reload()}
            />
            <Button variant="primary" icon={Upload} onClick={() => setUploadOpen((open) => !open)}>
              Загрузить
            </Button>
          </>
        }
      />

      {uploadOpen && (
        <Panel className="upload-panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">Загрузка музыки</h2>
              <p className="panel-subtitle">
                MP3, FLAC, OGG, M4A · до {maxUploadMb} МБ на файл
                {settings.error ? ' · лимит из настроек недоступен, показано значение по умолчанию' : ''}
              </p>
            </div>
            <IconButton icon={X} label="Свернуть загрузку" onClick={closeUpload} disabled={uploadRunning} />
          </div>

          <label className="dropzone">
            <input
              type="file"
              multiple
              accept={UPLOAD_ACCEPT}
              className="visually-hidden"
              onChange={(event) => {
                addFiles(Array.from(event.target.files ?? []));
                event.target.value = '';
              }}
            />
            <Upload size={22} aria-hidden="true" />
            <span className="dropzone__title">Перетащите файлы сюда или выберите на диске</span>
            <span className="dropzone__hint">
              Дубликаты и повреждённые файлы сервер отклонит — они останутся в списке с ошибкой
            </span>
          </label>

          {uploadItems.length > 0 && (
            <ul className="upload-list">
              {uploadItems.map((item) => (
                <li key={item.id} className={`upload-item upload-item--${item.status}`}>
                  <div className="upload-item__main">
                    <span className="upload-item__name" title={item.file.name}>
                      {item.file.name}
                    </span>
                    <span className="upload-item__meta">
                      {formatBytes(item.file.size)}
                      {item.error ? ` · ${item.error}` : ''}
                    </span>
                  </div>
                  {item.status === 'uploading' ? (
                    <div className="upload-item__progress">
                      <ProgressBar value={item.progress / 100} label={`Загрузка ${item.file.name}`} />
                      <span>{item.progress}%</span>
                    </div>
                  ) : item.status === 'done' ? (
                    <span className="upload-item__status upload-item__status--ok">
                      <Check size={14} aria-hidden="true" /> Готово
                    </span>
                  ) : item.status === 'duplicate' ? (
                    <span className="upload-item__status upload-item__status--warn">Уже в медиатеке</span>
                  ) : item.status === 'queued' ? (
                    <span className="upload-item__status">В очереди</span>
                  ) : (
                    <span className="upload-item__status upload-item__status--error">Ошибка</span>
                  )}
                  {item.status === 'error' && (
                    <IconButton
                      icon={RefreshCw}
                      label="Повторить загрузку файла"
                      size="sm"
                      onClick={() =>
                        setUploadItems((items) =>
                          items.map((entry) =>
                            entry.id === item.id
                              ? { ...entry, status: 'queued', progress: 0, error: null }
                              : entry,
                          ),
                        )
                      }
                    />
                  )}
                </li>
              ))}
            </ul>
          )}

          <div className="upload-panel__footer">
            <span className="muted">
              {uploadRunning ? (
                <span className="inline-loading">
                  <Spinner size={14} /> Загрузка…
                </span>
              ) : (
                `Готово: ${uploadStats.done} · ошибок: ${uploadStats.error + uploadStats.duplicate} · в очереди: ${uploadStats.queued}`
              )}
            </span>
            <div className="upload-panel__actions">
              {uploadStats.error > 0 && !uploadRunning && (
                <Button variant="secondary" size="sm" icon={RefreshCw} onClick={retryFailed}>
                  Повторить ошибки
                </Button>
              )}
              {(uploadRunning || uploadStats.queued > 0) && (
                <Button variant="ghost" size="sm" onClick={cancelUploads}>
                  Отменить
                </Button>
              )}
            </div>
          </div>
        </Panel>
      )}

      <div className="toolbar">
        <div className="search-field">
          <Search size={15} aria-hidden="true" />
          <TextField
            label="Поиск по медиатеке"
            hideLabel
            placeholder="Поиск по названию, исполнителю, альбому"
            value={searchInput}
            onChange={(event) => setSearchInput(event.target.value)}
          />
        </div>
        <div className="toolbar__sort">
          <SelectField
            label="Сортировка"
            hideLabel
            value={sort}
            onChange={(event) => setSort(event.target.value as TrackSortField)}
          >
            <option value="created">По дате добавления</option>
            <option value="title">По названию</option>
            <option value="artist">По исполнителю</option>
            <option value="album">По альбому</option>
            <option value="duration">По длительности</option>
          </SelectField>
          <IconButton
            icon={order === 'asc' ? ArrowUp : ArrowDown}
            label={order === 'asc' ? 'По возрастанию' : 'По убыванию'}
            onClick={() => setOrder(order === 'asc' ? 'desc' : 'asc')}
          />
        </div>
      </div>

      {selectedIds.length > 0 && (
        <div className="bulk-bar" role="region" aria-label="Действия с выбранными треками">
          <span className="bulk-bar__count">
            Выбрано: <strong>{selectedIds.length}</strong>
          </span>
          <div className="bulk-bar__actions">
            <Button
              variant="secondary"
              size="sm"
              icon={ListPlus}
              onClick={() => {
                setAddTargetPlaylist(playlists.data?.[0]?.id ?? '');
                setAddToPlaylistOpen(true);
              }}
            >
              В плейлист
            </Button>
            <Button variant="danger" size="sm" icon={Trash2} onClick={() => setBulkDeleteOpen(true)}>
              Удалить
            </Button>
            <Button variant="ghost" size="sm" onClick={() => setSelected(new Set())}>
              Снять выделение
            </Button>
          </div>
        </div>
      )}

      <Panel className="table-panel">
        {tracks.loading && !tracks.data ? (
          <LoadingState label="Загружаем медиатеку…" rows={6} />
        ) : tracks.error && !tracks.data ? (
          <ErrorState message={tracks.error} onRetry={() => void tracks.reload()} />
        ) : pageItems.length === 0 ? (
          query ? (
            <EmptyState
              icon={Search}
              title="Ничего не найдено"
              description={`По запросу «${query}» нет треков.`}
              action={
                <Button variant="secondary" size="sm" onClick={() => setSearchInput('')}>
                  Сбросить поиск
                </Button>
              }
            />
          ) : (
            <EmptyState
              icon={Music4}
              title="Медиатека пуста"
              description="Загрузите первые треки — они появятся в эфире и плейлистах."
              action={
                <Button variant="primary" size="sm" icon={Upload} onClick={() => setUploadOpen(true)}>
                  Загрузить музыку
                </Button>
              }
            />
          )
        ) : (
          <>
            <div className="table-scroll">
              <table className="table track-table">
                <thead>
                  <tr>
                    <th className="col-check">
                      <HeaderCheckbox
                        checked={allSelected}
                        indeterminate={!allSelected && someSelected}
                        onChange={(checked) => {
                          setSelected((current) => {
                            const next = new Set(current);
                            pageItems.forEach((track) => {
                              if (checked) next.add(track.id);
                              else next.delete(track.id);
                            });
                            return next;
                          });
                        }}
                      />
                    </th>
                    <th className="col-cover" aria-label="Обложка" />
                    <th>{sortHeader('title', 'Название')}</th>
                    <th className="col-artist">{sortHeader('artist', 'Исполнитель')}</th>
                    <th className="col-album">{sortHeader('album', 'Альбом')}</th>
                    <th className="col-duration">{sortHeader('duration', 'Длительность')}</th>
                    <th className="col-menu" aria-label="Действия" />
                  </tr>
                </thead>
                <tbody>
                  {pageItems.map((track) => {
                    const isSelected = selected.has(track.id);
                    const isPreview = previewTrack?.id === track.id;
                    return (
                      <tr key={track.id} className={isSelected ? 'is-selected' : undefined}>
                        <td className="col-check">
                          <input
                            type="checkbox"
                            checked={isSelected}
                            aria-label={`Выбрать трек ${track.title}`}
                            onChange={(event) => toggleSelected(track.id, event.target.checked)}
                          />
                        </td>
                        <td className="col-cover">
                          <button
                            type="button"
                            className="thumb-btn"
                            onClick={() => togglePreview(track)}
                            title={isPreview && previewPlaying ? 'Приостановить прослушивание' : 'Прослушать трек'}
                            aria-label={
                              isPreview && previewPlaying
                                ? `Приостановить ${track.title}`
                                : `Прослушать ${track.title}`
                            }
                          >
                            <Cover src={track.coverUrl} alt="" size="xs" />
                            <span className="thumb-btn__overlay" aria-hidden="true">
                              {isPreview && previewPlaying ? <Pause size={12} /> : <Play size={12} />}
                            </span>
                          </button>
                        </td>
                        <td>
                          <div className="track-cell">
                            <span className="track-cell__title" title={track.title}>
                              {track.title}
                            </span>
                            <span className="track-cell__sub">
                              {track.artist ?? '—'}
                              {track.album ? ` · ${track.album}` : ''}
                            </span>
                          </div>
                        </td>
                        <td className="col-artist">{track.artist ?? '—'}</td>
                        <td className="col-album" title={track.album ?? undefined}>
                          {track.album ?? '—'}
                        </td>
                        <td className="col-duration">{formatDuration(track.duration)}</td>
                        <td className="col-menu">
                          <MenuButton
                            label={`Действия с треком ${track.title}`}
                            items={[
                              {
                                label: isPreview && previewPlaying ? 'Пауза' : 'Слушать',
                                icon: isPreview && previewPlaying ? Pause : Play,
                                onSelect: () => togglePreview(track),
                              },
                              {
                                label: 'В эфир сейчас',
                                icon: Music4,
                                separatorBefore: true,
                                disabled: desired !== 'playing',
                                hint: 'Сначала запустите эфир',
                                onSelect: () =>
                                  void trackAction(
                                    () => api.admin.tracks.playNow(track.id),
                                    'Трек отправлен в эфир',
                                  ),
                              },
                              {
                                label: 'Следующим треком',
                                disabled: desired !== 'playing',
                                hint: 'Сначала запустите эфир',
                                onSelect: () =>
                                  void trackAction(
                                    () => api.admin.tracks.playNext(track.id),
                                    'Трек поставлен следующим',
                                  ),
                              },
                              {
                                label: 'В очередь',
                                onSelect: () =>
                                  void trackAction(
                                    () => api.admin.tracks.enqueue(track.id),
                                    'Трек добавлен в очередь',
                                  ),
                              },
                              {
                                label: 'Изменить метаданные',
                                separatorBefore: true,
                                onSelect: () => setEditTrack(track),
                              },
                              {
                                label: 'Удалить',
                                danger: true,
                                onSelect: () => setDeleteTarget(track),
                              },
                            ]}
                          />
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>

            <div className="table-footer">
              <span className="muted">
                Показано {formatCount(shownFrom)}–{formatCount(shownTo)} из {formatCount(total)}
              </span>
              <div className="table-footer__pages">
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={pageIndex === 0}
                  onClick={() => setPageIndex((index) => Math.max(0, index - 1))}
                >
                  Назад
                </Button>
                <Button
                  variant="ghost"
                  size="sm"
                  disabled={!hasNext}
                  onClick={() => setPageIndex((index) => index + 1)}
                >
                  Вперёд
                </Button>
              </div>
            </div>
          </>
        )}
      </Panel>

      {previewTrack && (
        <div className="preview-chip" role="status">
          <span className="preview-chip__icon">
            <Equalizer active={previewPlaying} />
          </span>
          <span className="preview-chip__text">
            <strong>{previewTrack.title}</strong>
            <span>{previewTrack.artist ?? '—'}</span>
          </span>
          <IconButton
            icon={previewPlaying ? Pause : Play}
            label={previewPlaying ? 'Пауза' : 'Продолжить прослушивание'}
            size="sm"
            onClick={() => togglePreview(previewTrack)}
          />
          <IconButton icon={X} label="Закрыть прослушивание" size="sm" onClick={stopPreview} />
        </div>
      )}

      {dragActive && (
        <div className="drop-overlay" aria-hidden="true">
          <div className="drop-overlay__card">
            <Upload size={28} />
            <span>Отпустите файлы, чтобы загрузить их в медиатеку</span>
            <span className="muted">MP3, FLAC, OGG, M4A</span>
          </div>
        </div>
      )}

      <audio
        ref={previewRef}
        preload="none"
        onPlay={() => setPreviewPlaying(true)}
        onPause={() => setPreviewPlaying(false)}
        onEnded={() => setPreviewPlaying(false)}
      />

      <Modal
        open={editTrack !== null}
        onClose={() => (editBusy ? undefined : setEditTrack(null))}
        title="Метаданные трека"
        description="Изменения сохраняются в базе и используются в эфире и интерфейсе."
        footer={
          <>
            <Button variant="ghost" onClick={() => setEditTrack(null)} disabled={editBusy}>
              Отмена
            </Button>
            <Button variant="primary" onClick={() => void saveMetadata()} loading={editBusy}>
              Сохранить
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <TextField
            label="Название"
            value={editForm.title}
            onChange={(event) => setEditForm({ ...editForm, title: event.target.value })}
          />
          <TextField
            label="Исполнитель"
            value={editForm.artist}
            onChange={(event) => setEditForm({ ...editForm, artist: event.target.value })}
          />
          <TextField
            label="Альбом"
            value={editForm.album}
            onChange={(event) => setEditForm({ ...editForm, album: event.target.value })}
          />
          <TextField
            label="Жанр"
            value={editForm.genre}
            onChange={(event) => setEditForm({ ...editForm, genre: event.target.value })}
          />
          <TextField
            label="Год"
            inputMode="numeric"
            value={editForm.year}
            onChange={(event) => setEditForm({ ...editForm, year: event.target.value })}
            hint="Необязательно, 1900–2100"
          />
          {editTrack && (
            <div className="form-note">
              Файл: {editTrack.id.slice(0, 8)} · {formatDuration(editTrack.duration)} ·{' '}
              {formatBytes(editTrack.size)}
            </div>
          )}
        </div>
      </Modal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Удалить трек?"
        description={
          deleteTarget ? (
            <>
              Трек «{deleteTarget.title}» будет удалён из медиатеки и всех плейлистов
              {deleteTarget.playlistsCount
                ? ` (${deleteTarget.playlistsCount} ${pluralRu(deleteTarget.playlistsCount, [
                    'плейлист',
                    'плейлиста',
                    'плейлистов',
                  ])})`
                : ''}
              . Если трек сейчас в эфире, удаление произойдёт после смены трека.
            </>
          ) : null
        }
        confirmLabel="Удалить"
        danger
        busy={deleteBusy}
        onConfirm={() => void confirmDelete()}
        onCancel={() => setDeleteTarget(null)}
      />

      <ConfirmDialog
        open={bulkDeleteOpen}
        title="Удалить выбранные треки?"
        description={`Будут удалены ${selectedIds.length} трек(ов) из медиатеки и всех плейлистов. Действие необратимо.`}
        confirmLabel="Удалить"
        danger
        busy={bulkBusy}
        onConfirm={() => void confirmBulkDelete()}
        onCancel={() => setBulkDeleteOpen(false)}
      />

      <Modal
        open={addToPlaylistOpen}
        onClose={() => (bulkBusy ? undefined : setAddToPlaylistOpen(false))}
        title="Добавить в плейлист"
        description={`Выбрано треков: ${selectedIds.length}`}
        footer={
          <>
            <Button variant="ghost" onClick={() => setAddToPlaylistOpen(false)} disabled={bulkBusy}>
              Отмена
            </Button>
            <Button
              variant="primary"
              onClick={() => void addSelectionToPlaylist()}
              loading={bulkBusy}
              disabled={!addTargetPlaylist}
            >
              Добавить
            </Button>
          </>
        }
      >
        {playlists.loading && !playlists.data ? (
          <LoadingState label="Загружаем плейлисты…" rows={2} />
        ) : playlists.data && playlists.data.length > 0 ? (
          <SelectField
            label="Плейлист"
            value={addTargetPlaylist}
            onChange={(event) => setAddTargetPlaylist(event.target.value)}
          >
            <option value="">Выберите плейлист</option>
            {playlists.data.map((playlist) => (
              <option key={playlist.id} value={playlist.id}>
                {playlist.name}
              </option>
            ))}
          </SelectField>
        ) : (
          <EmptyState compact title="Плейлистов нет" description="Создайте плейлист в разделе «Плейлисты»." />
        )}
      </Modal>
    </div>
  );
}
