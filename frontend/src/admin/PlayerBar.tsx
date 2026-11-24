import { useEffect, useRef, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  Pause,
  Play,
  Power,
  Repeat,
  Repeat1,
  RotateCw,
  Shuffle,
  SkipForward,
  Square,
} from 'lucide-react';
import { Cover } from '../components/Cover';
import { Equalizer, IconButton } from '../components/ui';
import { VolumeControl } from '../components/VolumeControl';
import { useToast } from '../components/Toasts';
import { useAudio } from '../lib/audio';
import { useLive } from '../lib/live';
import { useResource } from '../lib/hooks';
import { api, errorMessage } from '../lib/api';
import { repeatLabel } from '../lib/format';
import type { RepeatMode } from '../lib/types';

function nextRepeat(mode: RepeatMode): RepeatMode {
  if (mode === 'none') return 'all';
  if (mode === 'all') return 'one';
  return 'none';
}

/**
 * Постоянная нижняя панель. Локальное прослушивание (слева/в центре)
 * отделено от управления эфиром (справа) вертикальным разделителем.
 */
export function PlayerBar() {
  const { now, reload } = useLive();
  const audio = useAudio();
  const toast = useToast();
  const [busy, setBusy] = useState<string | null>(null);

  const station = now?.station ?? null;
  const track = now?.track ?? null;
  const desired = station?.desired ?? 'stopped';
  const sourcePlaylistId = now?.source?.playlistId ?? null;

  const playlistResource = useResource(
    () => (sourcePlaylistId ? api.admin.playlists.get(sourcePlaylistId) : Promise.resolve(null)),
    [sourcePlaylistId],
    { pollMs: 60000 },
  );
  const playlist = playlistResource.data?.playlist ?? null;
  const repeatMode: RepeatMode = playlist?.repeatMode ?? 'none';

  const lastAudioError = useRef<string | null>(null);
  useEffect(() => {
    if (audio.error && audio.error !== lastAudioError.current) {
      toast.notify('error', audio.error);
      lastAudioError.current = audio.error;
    }
    if (!audio.error) lastAudioError.current = null;
  }, [audio.error, toast]);

  const run = async (key: string, action: () => Promise<void>, message: string) => {
    setBusy(key);
    try {
      await action();
      toast.notify('success', message);
      reload();
      if (key === 'start' || key === 'stop' || key === 'restart') {
        window.setTimeout(reload, 2500);
      }
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const changeRepeat = async () => {
    if (!playlist) return;
    const target = nextRepeat(repeatMode);
    await run(
      'repeat',
      async () => {
        await api.admin.playlists.update(playlist.id, { repeat_mode: target });
        await playlistResource.reload(true);
      },
      `Режим повтора: ${repeatLabel(target).toLowerCase()}`,
    );
  };

  const airDisabled = busy !== null;

  return (
    <footer className="player-bar" aria-label="Панель прослушивания и управления эфиром">
      <div className="pb-track">
        <Link to="/admin" className="pb-track__cover" aria-label="Обзор станции">
          <Cover src={track?.coverUrl} alt={track ? `Обложка: ${track.title}` : 'Нет текущего трека'} size="sm" />
        </Link>
        <div className="pb-track__text">
          <span className="pb-track__title" title={track?.title ?? undefined}>
            {track?.title ?? 'Эфир не начался'}
          </span>
          <span className="pb-track__artist" title={track?.artist ?? undefined}>
            {track?.artist ?? (station ? 'Тишина в эфире' : 'Нет данных от сервера')}
          </span>
        </div>
        {audio.playing && (
          <span className="pb-track__eq" title="Локальное прослушивание">
            <Equalizer />
          </span>
        )}
      </div>

      <div className="pb-center">
        <div className="pb-controls">
          <IconButton
            icon={Shuffle}
            label="Перемешать будущую очередь"
            title="Перемешать будущую очередь (ручные вставки сохраняются)"
            disabled={airDisabled}
            onClick={() =>
              void run('shuffle', api.admin.queue.shuffle, 'Будущая очередь перемешана')
            }
          />
          <IconButton
            icon={repeatMode === 'one' ? Repeat1 : Repeat}
            label={
              playlist
                ? `Повтор: ${repeatLabel(repeatMode).toLowerCase()}. Нажмите, чтобы сменить`
                : 'Повтор недоступен: источник не является плейлистом'
            }
            title={
              playlist
                ? `Повтор: ${repeatLabel(repeatMode).toLowerCase()}. Нажмите, чтобы сменить`
                : 'Повтор доступен, когда источником эфира является плейлист'
            }
            active={repeatMode !== 'none'}
            disabled={!playlist || airDisabled}
            onClick={() => void changeRepeat()}
          />
          <IconButton
            icon={SkipForward}
            label={desired === 'playing' ? 'Следующий трек' : 'Сначала запустите эфир'}
            title={desired === 'playing' ? 'Пропустить текущий трек' : 'Сначала запустите эфир'}
            disabled={desired !== 'playing' || airDisabled}
            onClick={() => void run('skip', api.admin.station.skip, 'Пропуск передан движку')}
          />
        </div>

        <div className="pb-local">
          <IconButton
            icon={audio.playing ? Pause : Play}
            label={
              audio.canPlay
                ? audio.playing
                  ? 'Остановить локальное прослушивание'
                  : 'Слушать эфир локально'
                : 'Ссылка на поток ещё не получена'
            }
            title={
              audio.canPlay
                ? audio.playing
                  ? 'Остановить локальное прослушивание'
                  : 'Слушать эфир локально (эфир не меняется)'
                : 'Ссылка на поток ещё не получена'
            }
            tone="accent"
            active={audio.playing}
            disabled={!audio.canPlay || audio.busy}
            onClick={() => void audio.toggle()}
          />
          <span className="pb-local__label">Локально</span>
          <VolumeControl
            className="pb-volume"
            value={audio.volume}
            onChange={audio.setVolume}
            disabled={!audio.canPlay}
          />
        </div>
      </div>

      <div className="pb-air">
        <span className="pb-air__label">
          <span
            className={`dot ${desired === 'playing' ? 'dot--live' : 'dot--idle'}`}
            aria-hidden="true"
          />
          Эфир
        </span>
        <div className="pb-air__buttons">
          <IconButton
            icon={Power}
            label={desired === 'playing' ? 'Эфир уже запущен' : 'Запустить эфир'}
            title={desired === 'playing' ? 'Эфир уже запущен' : 'Запустить эфир'}
            tone="accent"
            active={desired === 'playing'}
            disabled={desired === 'playing' || airDisabled}
            onClick={() =>
              void run('start', api.admin.station.start, 'Эфир запускается')
            }
          />
          <IconButton
            icon={Square}
            label={desired === 'stopped' ? 'Эфир уже остановлен' : 'Остановить эфир'}
            title={desired === 'stopped' ? 'Эфир уже остановлен' : 'Остановить эфир'}
            disabled={desired === 'stopped' || airDisabled}
            onClick={() => void run('stop', api.admin.station.stop, 'Эфир останавливается')}
          />
          <IconButton
            icon={RotateCw}
            label={desired === 'stopped' ? 'Сначала запустите эфир' : 'Перезапустить движок'}
            title={
              desired === 'stopped'
                ? 'Сначала запустите эфир'
                : 'Перезапустить движок, сохранив состояние эфира'
            }
            disabled={desired === 'stopped' || airDisabled}
            onClick={() =>
              void run('restart', api.admin.station.restart, 'Движок перезапускается')
            }
          />
        </div>
      </div>
    </footer>
  );
}
