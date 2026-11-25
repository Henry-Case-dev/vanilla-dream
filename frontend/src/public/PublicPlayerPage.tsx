import { useMemo } from 'react';
import { Link } from 'react-router-dom';
import { Headphones, Play, Square } from 'lucide-react';
import { Logo } from '../components/Logo';
import { Cover } from '../components/Cover';
import { StatusPill } from '../components/StatusPill';
import { LinkList } from '../components/LinkList';
import { EmptyState, InlineError, LoadingState } from '../components/States';
import { CopyButton } from '../components/CopyButton';
import { Equalizer, IconButton, MetaRow, ProgressBar } from '../components/ui';
import { VolumeControl } from '../components/VolumeControl';
import { useAudio } from '../lib/audio';
import { useLive } from '../lib/live';
import { useResource } from '../lib/hooks';
import { api, publicLinksFromStation } from '../lib/api';
import { formatDuration, formatTime, formatUntil, sourceDisplayName } from '../lib/format';
import { describeStationStatus } from '../lib/status';

/**
 * Публичный плеер: без авторизации, только чтение.
 * Локальное прослушивание не меняет эфир, очередь и службы станции.
 */
export function PublicPlayerPage() {
  const { now, loading, error, feed, reload } = useLive();
  const audio = useAudio();
  const history = useResource(() => api.public.history(6), []);

  const station = now?.station ?? null;
  const track = now?.track ?? null;
  const sourceName = sourceDisplayName(now?.source);
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

  if (loading && !now) {
    return (
      <div className="public">
        <div className="public__glow" aria-hidden="true" />
        <header className="public__top">
          <Logo />
          <div className="public__top-right">
            <Link className="public__admin-link" to="/admin">
              Вход в панель
            </Link>
          </div>
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
          <div className="public__top-right">
            <Link className="public__admin-link" to="/admin">
              Вход в панель
            </Link>
          </div>
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
          <Link className="public__admin-link" to="/admin">
            Вход в панель
          </Link>
        </div>
      </header>

      <main className="public__main">
        <section className="public-hero" aria-labelledby="now-title">
          <div className="public-hero__cover">
            <Cover src={track?.coverUrl} alt={track ? `Обложка: ${track.title}` : 'Обложка эфира'} size="hero" rounded="lg" />
            {audio.playing && (
              <span className="public-hero__live">
                <Equalizer /> локально
              </span>
            )}
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
              {sourceName && (
                <span className="meta-item">
                  Источник: {now?.source?.kind === 'override' ? `Ручной выбор · ${sourceName}` : sourceName}
                </span>
              )}
              {station?.bitrate ? <span className="meta-item">{station.bitrate} кбит/с</span> : null}
              {now?.listeners.available ? (
                <span className="meta-item">{now.listeners.count ?? 0} слушателей</span>
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
                onClick={() => void audio.toggle()}
                disabled={!audio.canPlay || audio.busy}
                aria-label={audio.playing ? 'Остановить локальное прослушивание' : 'Слушать эфир'}
                title={
                  audio.canPlay
                    ? audio.playing
                      ? 'Остановить локальное прослушивание'
                      : 'Слушать эфир'
                    : 'Ссылка на поток ещё не получена'
                }
              >
                {audio.playing ? <Square size={22} /> : <Play size={24} />}
              </button>
              <div className="public-hero__controls-text">
                <span className="public-hero__controls-title">
                  {!audio.canPlay
                    ? audio.unavailableReason ?? 'Прослушивание недоступно'
                    : audio.busy
                      ? 'Подключение…'
                      : audio.state === 'reconnecting'
                        ? 'Переподключение…'
                        : audio.playing
                          ? 'Играет локально'
                          : 'Слушать эфир'}
                </span>
                <span className="public-hero__controls-sub">Звук только в вашем браузере</span>
              </div>
              <VolumeControl value={audio.volume} onChange={audio.setVolume} disabled={!audio.canPlay} />
            </div>

            {audio.error && <InlineError message={audio.error} />}
            {!audio.canPlay && !audio.error && (
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
            <h2 id="links-title">Слушать в других плеерах</h2>
            <p>Прямые ссылки для интернет-радиоплееров и совместимых приложений.</p>
          </div>
          {links.length > 0 ? (
            <LinkList links={links} />
          ) : (
            <EmptyState compact title="Ссылки недоступны" description="Сервер ещё не сообщил адреса трансляции." />
          )}
          {station?.baseUrl && (
            <p className="public-section__footnote">
              Публичный адрес станции:{' '}
              <span className="public-section__url">{station.baseUrl}</span>
              <CopyButton value={station.baseUrl} label="Копировать адрес" />
            </p>
          )}
        </section>

        <section className="public-section" aria-labelledby="history-title">
          <div className="public-section__head">
            <h2 id="history-title">Недавно в эфире</h2>
            <p>Последние треки и время выхода.</p>
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
        <span>
          Vanilla Dream Radio · {feed === 'live' ? 'обновляется автоматически' : 'обновление опросом'}
        </span>
        <span className="public__footer-right">
          <IconButton
            icon={Headphones}
            label={
              audio.playing
                ? 'Остановить локальное прослушивание'
                : 'Слушать эфир локально'
            }
            title="Локальное прослушивание не меняет эфир"
            size="sm"
            className="public__footer-icon"
            onClick={() => void audio.toggle()}
            disabled={!audio.canPlay}
          />
          <Link to="/admin" className="public__footer-link">
            Панель управления
          </Link>
        </span>
      </footer>
    </div>
  );
}
