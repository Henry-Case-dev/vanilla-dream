import { useEffect, useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  ArrowUpToLine,
  ChevronDown,
  ChevronUp,
  Clock,
  Disc3,
  GripVertical,
  Link2,
  ListMusic,
  Shuffle,
  Trash2,
  Users,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { PageHeader, Panel, ProgressBar, Button, IconButton, Chip, MetaRow } from '../components/ui';
import { Cover } from '../components/Cover';
import { StatusPill } from '../components/StatusPill';
import { LinkList } from '../components/LinkList';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { ConfirmDialog } from '../components/Modal';
import { SourceControl } from '../components/SourceControl';
import { useToast } from '../components/Toasts';
import { useLive, effectiveControl } from '../lib/live';
import { usePlaylistSettings } from '../lib/playlist-settings';
import { useResource, useTick } from '../lib/hooks';
import { api, errorMessage } from '../lib/api';
import {
  formatBytes,
  formatCount,
  formatDuration,
  formatRelativeMoment,
  formatTime,
  sourceDisplayName,
  sourceKindLabel,
} from '../lib/format';
import { describeStationStatus } from '../lib/status';
import { useDataChanged } from '../lib/refresh';
import { ManualOverrideBanner } from '../components/ManualOverrideBanner';
import type { HistoryEntry, QueueItem } from '../lib/types';

function Metric({
  icon: Icon,
  label,
  value,
  hint,
}: {
  icon: LucideIcon;
  label: string;
  value: string;
  hint?: string;
}) {
  return (
    <div className="metric">
      <span className="metric__icon" aria-hidden="true">
        <Icon size={16} />
      </span>
      <div className="metric__text">
        <span className="metric__label">{label}</span>
        <span className="metric__value">{value}</span>
        {hint && <span className="metric__hint">{hint}</span>}
      </div>
    </div>
  );
}

function queueSourceLabel(item: QueueItem): string {
  if (item.manual || item.sourceKind === 'manual') return 'Ручная вставка';
  if (item.sourceKind === 'override') return 'Временное переключение';
  if (item.sourceKind === 'library' || item.sourceLabel === 'library') return 'Вся медиатека';
  if (item.sourceKind === 'rotation') return 'Ротация';
  if (item.sourceKind === 'schedule') return item.sourceLabel || 'Расписание';
  if (item.sourceKind === 'playlist') {
    return item.sourceLabel || sourceDisplayName({ kind: 'playlist', name: item.sourceLabel }) || 'Плейлист';
  }
  return item.sourceLabel || (item.sourceKind ? sourceKindLabel(item.sourceKind) : 'Источник');
}

function QueueRow({
  item,
  index,
  total,
  current = false,
  timeZone,
  busy,
  onMove,
  onMoveNext,
  onRemove,
  dragFrom,
  onDragFrom,
  onDropAt,
  dropIndex,
  setDropIndex,
}: {
  item: QueueItem;
  index: number;
  total: number;
  current?: boolean;
  timeZone?: string;
  busy: boolean;
  onMove: (id: string, target: number) => void;
  onMoveNext: (id: string) => void;
  onRemove: (item: QueueItem) => void;
  dragFrom: number | null;
  onDragFrom: (index: number | null) => void;
  onDropAt: (index: number) => void;
  dropIndex: number | null;
  setDropIndex: (index: number | null) => void;
}) {
  const canDrag = !current && !busy;
  return (
    <div
      className={`queue-row ${current ? 'queue-row--current' : ''} ${
        dropIndex === index && !current ? 'is-drop-target' : ''
      }`.trim()}
      draggable={canDrag}
      onDragStart={(event) => {
        if (!canDrag) return;
        event.dataTransfer.effectAllowed = 'move';
        onDragFrom(index);
      }}
      onDragEnd={() => {
        onDragFrom(null);
        setDropIndex(null);
      }}
      onDragOver={(event) => {
        if (!canDrag || dragFrom === null) return;
        event.preventDefault();
        if (dropIndex !== index) setDropIndex(index);
      }}
      onDrop={(event) => {
        if (!canDrag || dragFrom === null) return;
        event.preventDefault();
        onDropAt(index);
      }}
    >
      <span className="queue-row__index">
        {current ? 'Сейчас' : canDrag ? <GripVertical size={12} className="queue-row__grip" aria-hidden="true" /> : null}
        {!current && index + 1}
      </span>
      <Cover src={item.track?.coverUrl} alt={item.track ? `Обложка: ${item.track.title}` : 'Обложка'} size="xs" />
      <div className="queue-row__text">
        <span className="queue-row__title">{item.track?.title ?? 'Трек удалён'}</span>
        <span className="queue-row__sub">
          {item.track?.artist ?? '—'} · {queueSourceLabel(item)}
        </span>
      </div>
      <span className="queue-row__time">
        {item.estimatedStart ? formatTime(item.estimatedStart, timeZone) : '—'}
      </span>
      {!current && (
        <div className="queue-row__actions">
          <IconButton
            icon={ChevronUp}
            label="Переместить выше"
            title="Переместить выше"
            size="sm"
            disabled={busy || index === 0}
            onClick={() => onMove(item.id, index - 1)}
          />
          <IconButton
            icon={ChevronDown}
            label="Переместить ниже"
            title="Переместить ниже"
            size="sm"
            disabled={busy || index === total - 1}
            onClick={() => onMove(item.id, index + 1)}
          />
          <IconButton
            icon={ArrowUpToLine}
            label="Поставить следующим"
            title="Поставить следующим"
            size="sm"
            disabled={busy || index === 0}
            onClick={() => onMoveNext(item.id)}
          />
          <IconButton
            icon={Trash2}
            label="Убрать из очереди"
            title="Убрать из очереди"
            size="sm"
            tone="danger"
            disabled={busy}
            onClick={() => onRemove(item)}
          />
        </div>
      )}
    </div>
  );
}

function HistoryList({ entries, timeZone }: { entries: HistoryEntry[]; timeZone?: string }) {
  if (entries.length === 0) {
    return <EmptyState compact title="История пуста" description="Записи появятся после первых треков в эфире." />;
  }
  return (
    <ul className="history-list">
      <li className="history-row history-row--head" aria-hidden="true">
        <span className="history-row__time">Время</span>
        <span className="history-row__title">Трек</span>
        <span className="history-row__artist">Исполнитель</span>
        <span className="history-row__source">Источник</span>
      </li>
      {entries.map((entry, index) => (
        <li key={`${entry.trackId ?? entry.title}-${entry.startedAt ?? index}`} className="history-row">
          <span className="history-row__time">{formatTime(entry.startedAt, timeZone)}</span>
          <span className="history-row__title">{entry.title}</span>
          <span className="history-row__artist">{entry.artist ?? '—'}</span>
          <span className="history-row__source">
            {entry.sourceKind ? sourceKindLabel(entry.sourceKind) : '—'}
          </span>
        </li>
      ))}
    </ul>
  );
}

export function OverviewPage() {
  const { now, control, loading, error, reload, queueRevision, historyRevision } = useLive();
  const toast = useToast();
  const settingsStore = usePlaylistSettings();
  useTick(30000);

  const queue = useResource(() => api.admin.queue.get(), [], { pollMs: 30000 });
  const links = useResource(() => api.admin.links(), []);
  const history = useResource(() => api.admin.history(8), [], { pollMs: 60000 });
  const library = useResource(() => api.admin.tracks.list({ limit: 1 }), [], { pollMs: 60000 });
  const settings = useResource(() => api.admin.settings.get(), []);

  const [busyQueue, setBusyQueue] = useState(false);
  const [clearManualOpen, setClearManualOpen] = useState(false);
  const [dragFrom, setDragFrom] = useState<number | null>(null);
  const [dropIndex, setDropIndex] = useState<number | null>(null);

  const timeZone = settings.data?.timezone ?? undefined;
  const station = now?.station ?? null;
  const track = now?.track ?? null;
  const statusView = describeStationStatus(station?.status, station?.reason);
  const state = effectiveControl(control, now);
  const program = settingsStore.get(state?.programPlaylistId ?? null);
  const repeatOne = program?.repeatMode === 'one';

  useEffect(() => {
    if (queueRevision === null) return;
    void queue.reload(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [queueRevision]);

  useEffect(() => {
    if (historyRevision === null && !track?.startedAt) return;
    void history.reload(true);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [historyRevision, track?.startedAt]);

  useDataChanged(() => {
    reload();
    void queue.reload(true);
    void library.reload(true);
    void history.reload(true);
    void links.reload(true);
    void settings.reload(true);
  });

  const progress = useMemo(() => {
    if (!track) return null;
    if (typeof track.progress === 'number' && track.progress >= 0) {
      return Math.min(1, track.progress > 1 ? track.progress / 100 : track.progress);
    }
    if (track.elapsed !== null && track.duration) return Math.min(1, track.elapsed / track.duration);
    return null;
  }, [track]);

  const radioLinks = links.data ?? [];
  const queueItems = queue.data?.items ?? [];
  const hasManual = queueItems.some((item) => item.manual);

  const moveItem = async (id: string, target: number) => {
    if (target < 0 || target >= queueItems.length) return;
    setBusyQueue(true);
    try {
      const result = await api.admin.queue.move(id, target);
      await queue.reload(true);
      if (result.engineSynced === false) {
        toast.notify('info', 'Порядок сохранён, применяется движком…');
      }
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusyQueue(false);
    }
  };

  const moveNext = async (id: string) => {
    setBusyQueue(true);
    try {
      const result = await api.admin.queue.moveNext(id);
      await queue.reload(true);
      if (result.engineSynced === false) {
        toast.notify('info', 'Позиция сохранена, применяется движком…');
      }
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusyQueue(false);
    }
  };

  const removeItem = async (item: QueueItem) => {
    setBusyQueue(true);
    try {
      const result = await api.admin.queue.remove(item.id);
      toast.notify(
        result.engineSynced === false ? 'info' : 'success',
        result.engineSynced === false
          ? 'Удаление сохранено, применяется движком…'
          : 'Трек убран из очереди',
      );
      await queue.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusyQueue(false);
    }
  };

  const shuffleQueue = async () => {
    if (repeatOne) {
      toast.notify('info', 'При повторе трека разовое перемешивание недоступно');
      return;
    }
    setBusyQueue(true);
    try {
      const result = await api.admin.queue.shuffle();
      toast.notify(
        result.engineSynced === false ? 'info' : 'success',
        result.engineSynced === false
          ? 'Порядок сохранён, применяется движком…'
          : 'Ближайшие автоматические треки перемешаны',
      );
      await queue.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusyQueue(false);
    }
  };

  const clearManual = async () => {
    setBusyQueue(true);
    try {
      const result = await api.admin.queue.clearManual();
      toast.notify(
        result.engineSynced === false ? 'info' : 'success',
        result.engineSynced === false
          ? 'Очистка сохранена, применяется движком…'
          : 'Ручные добавления удалены из очереди',
      );
      setClearManualOpen(false);
      await queue.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusyQueue(false);
    }
  };

  const dropAt = (index: number) => {
    const from = dragFrom;
    setDragFrom(null);
    setDropIndex(null);
    if (from === null || from === index) return;
    const item = queueItems[from];
    if (!item) return;
    void moveItem(item.id, index);
  };

  if (loading && !now) {
    return (
      <div className="page">
        <PageHeader title="Радиостанция" subtitle="Текущее состояние эфира" />
        <LoadingState label="Загружаем состояние эфира…" rows={4} />
      </div>
    );
  }

  if (error && !now) {
    return (
      <div className="page">
        <PageHeader title="Радиостанция" subtitle="Текущее состояние эфира" />
        <ErrorState message={error} onRetry={reload} />
      </div>
    );
  }

  return (
    <div className="page">
      <PageHeader
        title="Радиостанция"
        subtitle={
          <span className="page-header__status">
            <StatusPill status={station?.status ?? 'unknown'} reason={station?.reason} />
            {statusView.tone === 'error' && <span className="muted">{statusView.detail}</span>}
          </span>
        }
        actions={
          <Link className="btn btn--ghost btn--md" to="/">
            Публичный плеер
          </Link>
        }
      />

      <ManualOverrideBanner />
      <SourceControl />

      <Panel className="np-hero">
        <div className="np-hero__cover">
          <Cover
            src={track?.coverUrl}
            alt={track ? `Обложка: ${track.title}` : 'Нет текущего трека'}
            size="hero"
            rounded="lg"
          />
        </div>
        <div className="np-hero__info">
          <span className="eyebrow">Сейчас в эфире</span>
          <h2 className="np-hero__title">{track?.title ?? statusView.label}</h2>
          <p className="np-hero__artist">{track?.artist ?? statusView.detail}</p>
          {track?.album && <p className="np-hero__album">{track.album}</p>}

          {track && (
            <div className="np-hero__progress">
              <ProgressBar value={progress ?? 0} label="Прогресс трека" />
              <div className="np-hero__times">
                <span>{formatDuration(track.elapsed)}</span>
                <span>{formatDuration(track.duration)}</span>
              </div>
            </div>
          )}

          <MetaRow>
            <Chip tone="accent">{track ? 'Трек в эфире' : 'Нет трека'}</Chip>
            {station?.bitrate ? <span className="meta-item">{station.bitrate} кбит/с</span> : null}
            {track?.startedAt && (
              <span className="meta-item">Начало: {formatTime(track.startedAt, timeZone)}</span>
            )}
          </MetaRow>

          {now?.nextTrack && (
            <div className="np-hero__next">
              <span className="np-hero__next-label">Далее</span>
              <span className="np-hero__next-title">{now.nextTrack.title}</span>
              <span className="muted">{now.nextTrack.artist ?? '—'}</span>
              <span className="muted">{formatDuration(now.nextTrack.duration)}</span>
            </div>
          )}
        </div>
      </Panel>

      <div className="metrics">
        <Metric
          icon={Users}
          label="Слушатели"
          value={
            now
              ? now.listeners.available
                ? formatCount(now.listeners.count ?? 0)
                : '—'
              : '—'
          }
          hint={now?.listeners.available ? 'Icecast, текущее соединение' : 'Статистика Icecast недоступна'}
        />
        <Metric
          icon={Disc3}
          label="Треков в медиатеке"
          value={library.loading && !library.data ? '…' : library.data ? formatCount(library.data.total) : '—'}
          hint={library.data ? formatBytes(library.data.library.bytes) : 'Данные недоступны'}
        />
        <Metric
          icon={Clock}
          label="Следующее переключение"
          value={
            settings.data && !settings.data.scheduleEnabled
              ? 'Расписание выкл.'
              : now?.nextScheduleChange
                ? formatRelativeMoment(now.nextScheduleChange.at, timeZone)
                : now?.track
                  ? 'После текущего трека'
                  : '—'
          }
          hint={
            now?.nextScheduleChange?.ruleName ??
            (settings.data && !settings.data.scheduleEnabled
              ? 'Эфир идёт по ротации или плейлисту по умолчанию'
              : 'Нет ближайших границ расписания')
          }
        />
      </div>

      <div className="overview-grid">
        <Panel className="queue-panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">Следующие треки эфира</h2>
              <p className="panel-subtitle">Порядок будущих позиций; ручные вставки сохраняются</p>
            </div>
            <div className="panel-head__actions">
              <Button
                variant="ghost"
                size="sm"
                icon={Shuffle}
                onClick={() => void shuffleQueue()}
                disabled={busyQueue || repeatOne}
                title={
                  repeatOne
                    ? 'При повторе трека разовое перемешивание недоступно'
                    : 'Перемешать ближайшие автоматические треки один раз'
                }
              >
                Перемешать
              </Button>
              {hasManual && (
                <Button variant="ghost" size="sm" onClick={() => setClearManualOpen(true)} disabled={busyQueue}>
                  Очистить ручные добавления
                </Button>
              )}
            </div>
          </div>

          {queue.loading && !queue.data ? (
            <LoadingState label="Загружаем очередь…" rows={3} />
          ) : queue.error && !queue.data ? (
            <ErrorState message={queue.error} onRetry={() => void queue.reload()} />
          ) : queue.data && (queue.data.current || queueItems.length > 0) ? (
            <div className="queue-list">
              {queue.data.current && station?.status !== 'off' && (
                <QueueRow
                  item={queue.data.current}
                  index={0}
                  total={1}
                  current
                  timeZone={timeZone}
                  onMove={moveItem}
                  onMoveNext={moveNext}
                  onRemove={removeItem}
                  busy
                  dragFrom={dragFrom}
                  onDragFrom={setDragFrom}
                  onDropAt={dropAt}
                  dropIndex={dropIndex}
                  setDropIndex={setDropIndex}
                />
              )}
              {queueItems.map((item, index) => (
                <QueueRow
                  key={item.id}
                  item={item}
                  index={index}
                  total={queueItems.length}
                  timeZone={timeZone}
                  onMove={moveItem}
                  onMoveNext={moveNext}
                  onRemove={removeItem}
                  busy={busyQueue}
                  dragFrom={dragFrom}
                  onDragFrom={setDragFrom}
                  onDropAt={dropAt}
                  dropIndex={dropIndex}
                  setDropIndex={setDropIndex}
                />
              ))}
            </div>
          ) : (
            <EmptyState
              compact
              icon={ListMusic}
              title="Очередь пуста"
              description="Запустите эфир или добавьте треки из медиатеки."
            />
          )}
        </Panel>

        <Panel className="links-panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">
                <Link2 size={16} aria-hidden="true" /> Ссылки на трансляцию
              </h2>
              <p className="panel-subtitle">Основная ссылка — прямой MP3 для плееров и приложений</p>
            </div>
          </div>
          {links.loading && !links.data ? (
            <LoadingState label="Загружаем ссылки…" rows={2} />
          ) : links.error && !links.data ? (
            <ErrorState message={links.error} onRetry={() => void links.reload()} />
          ) : radioLinks.length > 0 ? (
            <LinkList links={radioLinks} />
          ) : (
            <EmptyState compact icon={Link2} title="Ссылки недоступны" description="Сервер ещё не сообщил адреса трансляции." />
          )}
        </Panel>
      </div>

      <Panel className="history-panel">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">История эфира</h2>
            <p className="panel-subtitle">Последние треки и источники</p>
          </div>
        </div>
        {history.loading && !history.data ? (
          <LoadingState label="Загружаем историю…" rows={3} />
        ) : history.error && !history.data ? (
          <ErrorState message={history.error} onRetry={() => void history.reload()} />
        ) : (
          <HistoryList entries={history.data ?? []} timeZone={timeZone} />
        )}
      </Panel>

      <ConfirmDialog
        open={clearManualOpen}
        title="Очистить ручные добавления?"
        description="Из будущей очереди будут удалены все треки, добавленные вручную. Текущий трек продолжит играть."
        confirmLabel="Очистить"
        danger
        busy={busyQueue}
        onConfirm={() => void clearManual()}
        onCancel={() => setClearManualOpen(false)}
      />
    </div>
  );
}
