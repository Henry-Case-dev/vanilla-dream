import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { Headphones, Pause, Play, RotateCw, Square, X } from 'lucide-react';
import { Logo } from '../components/Logo';
import { Cover } from '../components/Cover';
import { StatusPill } from '../components/StatusPill';
import { LinkList } from '../components/LinkList';
import { EmptyState, InlineError, LoadingState } from '../components/States';
import { IconButton, MetaRow, ProgressBar } from '../components/ui';
import { VolumeControl } from '../components/VolumeControl';
import { useAudio } from '../lib/audio';
import { useLive } from '../lib/live';
import { useResource } from '../lib/hooks';
import { api, publicLinksFromStation } from '../lib/api';
import { formatDuration, formatTime, formatUntil, effectiveSourceName, pluralRu } from '../lib/format';
import { describeStationStatus } from '../lib/status';

/**
 * Публичный плеер: без авторизации, только чтение.
 * Локальное прослушивание не меняет эфир, очередь и службы станции.
 */
export function PublicPlayerPage() {
  const { now, loading, error, reload } = useLive();
  const audio = useAudio();

  const station = now?.station ?? null;
  const track = now?.track ?? null;
  const historySignal = now?.historyRevision ?? track?.startedAt ?? null;
  const history = useResource(() => api.public.history(6), [historySignal]);
  const sourceName = effectiveSourceName(station?.effectiveSource ?? now?.source);
  const statusView = describeStationStatus(station?.status, station?.reason);

  const links = useMemo(() => {
    if (!station) return [];
    return publicLinksFromStation(station);
  }, [station]);

  const progress = useMemo(() => {
    if (!track) return null;
    if (typeof track.progress === 'number' && track.progress >= 0) {
      const value = track.progress > 1 ? track.progress / 100 : track.progress;
      return Math.min(1, value);
    }
    if (track.elapsed !== null && track.duration) return Math.min(1, track.elapsed / track.duration);
    return null;
  }, [track]);

  const localBusy = audio.state === 'connecting' || audio.state === 'reconnecting';
  const localFailed = audio.state === 'unavailable';
  const localLabel =
    audio.state === 'connecting'
      ? 'Подключение…'
      : audio.state === 'buffering'
        ? 'Буферизация…'
        : audio.state === 'reconnecting'
          ? 'Переподключение…'
          : audio.state === 'station_off'
            ? 'Эфир остановлен'
            : audio.state === 'blocked'
              ? 'Нажмите Play, чтобы продолжить'
              : audio.state === 'unavailable'
                ? 'Поток недоступен'
                : audio.state === 'playing'
                  ? 'Слушаете эфир'
                  : audio.canPlay
                    ? 'Слушать эфир'
                    : audio.unavailableReason ?? 'Прослушивание недоступно';

  const localClick = () => {
    if (localBusy) {
      audio.cancel();
      return;
    }
    if (localFailed || audio.state === 'blocked') {
      audio.reconnect();
      return;
    }
    void audio.toggle();
  };

  const playTitle = audio.playing ? 'Остановить прослушивание' : localLabel;

  if (loading && !now) {
    return (
      <div className="public">
        <div className="public__glow" aria-hidden="true" />
        <header className="public__top">
          <Logo />
        </header>
        <main className="public__main">
          <LoadingState label="Подключаемся к эфиру…" rows={3} />
        </main>
      </div>
    );
  }

  if (error && !now) {
    return (
      <div className="public">
        <div className="public__glow" aria-hidden="true" />
        <header className="public__top">
          <Logo />
        </header>
        <main className="public__main">
          <div className="public__offline">
            <EmptyState
              icon={Headphones}
              title="Эфир временно недоступен"
              description={error}
              action={
                <button type="button" className="btn btn--primary btn--md" onClick={reload}>
                  <span className="btn__label">Попробовать снова</span>
                </button>
              }
            />
          </div>
        </main>
      </div>
    );
  }

  return (
    <div className="public">
      <div className="public__glow" aria-hidden="true" />

      <header className="public__top">
        <Logo />
        <div className="public__top-right">
          {station && <StatusPill status={station.status} reason={station.reason} />}
        </div>
      </header>

      <main className="public__main">
        <section className="public-hero" aria-labelledby="now-title">
          <div className="public-hero__cover">
            <Cover src={track?.coverUrl} alt={track ? `Обложка: ${track.title}` : 'Обложка эфира'} size="hero" rounded="lg" />
          </div>

          <div className="public-hero__info">
            <span className="eyebrow">{track ? 'Сейчас в эфире' : 'Состояние эфира'}</span>
            <h1 className="public-hero__title" id="now-title">
              {track ? track.title : statusView.label}
            </h1>
            <p className="public-hero__artist">
              {track?.artist ?? statusView.detail}
              {track?.album ? <span className="public-hero__album"> · {track.album}</span> : null}
            </p>

            {track && progress !== null && (
              <div className="public-hero__progress">
                <ProgressBar value={progress} label="Прогресс текущего трека" />
                <div className="public-hero__times">
                  <span>{formatDuration(track.elapsed)}</span>
                  <span>{formatDuration(track.duration)}</span>
                </div>
              </div>
            )}

            <MetaRow className="public-hero__meta">
              {sourceName && <span className="meta-item">Источник: {sourceName}</span>}
              {station?.bitrate ? <span className="meta-item">{station.bitrate} кбит/с</span> : null}
              {now?.listeners.available ? (
                <span className="meta-item">
                  {now.listeners.count ?? 0} {pluralRu(now.listeners.count ?? 0, ['слушатель', 'слушателя', 'слушателей'])}
                </span>
              ) : (
                <span className="meta-item meta-item--muted">Статистика слушателей недоступна</span>
              )}
              {now?.nextScheduleChange && (
                <span className="meta-item">
                  Переключение: {formatUntil(now.nextScheduleChange.at)}
                  {now.nextScheduleChange.ruleName ? ` · ${now.nextScheduleChange.ruleName}` : ''}
                </span>
              )}
            </MetaRow>

            <div className="public-hero__controls">
              <button
                type="button"
                className={`play-fab ${audio.playing ? 'is-playing' : ''}`.trim()}
                onClick={localClick}
                disabled={!audio.canPlay && audio.intent === 'idle' && audio.state !== 'station_off'}
                aria-label={
                  localBusy
                    ? 'Отменить подключение'
                    : localFailed || audio.state === 'blocked'
                      ? 'Переподключиться к потоку'
                      : audio.playing
                        ? 'Остановить прослушивание'
                        : 'Слушать эфир'
                }
                title={playTitle}
              >
                {localBusy ? (
                  <X size={22} />
                ) : audio.state === 'station_off' && audio.intent !== 'idle' ? (
                  <Square size={20} />
                ) : localFailed ? (
                  <RotateCw size={22} />
                ) : audio.state === 'buffering' ? (
                  <Pause size={22} />
                ) : audio.playing ? (
                  <Square size={22} />
                ) : (
                  <Play size={24} />
                )}
              </button>
              <div className="public-hero__controls-text">
                <span className="public-hero__controls-title">{localLabel}</span>
              </div>
              <VolumeControl value={audio.volume} onChange={audio.setVolume} disabled={!audio.canPlay} />
            </div>

            {audio.error && (audio.state === 'blocked' || audio.state === 'unavailable') && (
              <InlineError message={audio.error} />
            )}
            {audio.state === 'reconnecting' && !audio.error && (
              <p className="public-hero__hint">Переподключаемся к потоку…</p>
            )}
            {!audio.canPlay && audio.intent === 'idle' && (
              <p className="public-hero__hint">
                {audio.unavailableReason === 'Сначала запустите эфир'
                  ? 'Локальное прослушивание включается после старта эфира.'
                  : audio.unavailableReason}
              </p>
            )}
          </div>
        </section>

        <section className="public-section" aria-labelledby="links-title">
          <div className="public-section__head">
            <h2 id="links-title">Прямые ссылки для интернет-радиоплееров и совместимых приложений</h2>
          </div>
          {links.length > 0 ? (
            <LinkList links={links} />
          ) : (
            <EmptyState compact title="Ссылки недоступны" description="Сервер ещё не сообщил адреса трансляции." />
          )}
        </section>

        <section className="public-section" aria-labelledby="history-title">
          <div className="public-section__head">
            <h2 id="history-title">Недавно в эфире</h2>
          </div>
          {history.loading && !history.data ? (
            <LoadingState label="Загружаем историю…" rows={2} />
          ) : history.error ? (
            <p className="public-section__hint">История временно недоступна.</p>
          ) : history.data && history.data.length > 0 ? (
            <ul className="public-history">
              {history.data.map((entry, index) => (
                <li key={`${entry.trackId ?? entry.title}-${entry.startedAt ?? index}`} className="public-history__row">
                  <span className="public-history__time">{formatTime(entry.startedAt)}</span>
                  <span className="public-history__title">{entry.title}</span>
                  <span className="public-history__artist">{entry.artist ?? '—'}</span>
                </li>
              ))}
            </ul>
          ) : (
            <p className="public-section__hint">История пока пуста — эфир только начинается.</p>
          )}
        </section>
      </main>

      <footer className="public__footer">
        <span>Vanilla Dream Radio</span>
        <span className="public__footer-right">
          <IconButton
            icon={Headphones}
            label={audio.playing ? 'Остановить прослушивание' : 'Слушать эфир'}
            title={audio.playing ? 'Остановить прослушивание' : 'Слушать эфир'}
            size="sm"
            className="public__footer-icon"
            onClick={localClick}
            disabled={!audio.canPlay && audio.intent === 'idle'}
            loading={localBusy}
          />
          <Link to="/admin" className="public__footer-link">
            Панель управления
          </Link>
        </span>
      </footer>
    </div>
  );
}
