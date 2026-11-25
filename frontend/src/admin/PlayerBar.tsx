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
  X,
} from 'lucide-react';
import { Cover } from '../components/Cover';
import { Equalizer, IconButton } from '../components/ui';
import { VolumeControl } from '../components/VolumeControl';
import { ConfirmDialog } from '../components/Modal';
import { useToast } from '../components/Toasts';
import { useAudio } from '../lib/audio';
import { useLive, effectiveControl } from '../lib/live';
import { usePlaylistSettings } from '../lib/playlist-settings';
import { api, errorMessage } from '../lib/api';
import { describeStationStatus, statusDotClass } from '../lib/status';
import { notifyDataChanged } from '../lib/refresh';
import type { RepeatMode } from '../lib/types';

function repeatTooltip(mode: RepeatMode, target: string | null): string {
  const where = target ? ` «${target}»` : '';
  if (mode === 'none') return `Без повтора${where}. Включить повтор плейлиста`;
  if (mode === 'all') return `Повтор плейлиста${where}. Включить повтор трека`;
  return `Повтор трека${where}. Выключить повтор`;
}

function localLabel(audio: ReturnType<typeof useAudio>, canPlay: boolean): string {
  switch (audio.state) {
    case 'connecting':
      return 'Подключение…';
    case 'buffering':
      return 'Буферизация…';
    case 'reconnecting':
      return 'Переподключение…';
    case 'station_off':
      return 'Эфир остановлен';
    case 'blocked':
      return 'Нажмите Play';
    case 'unavailable':
      return 'Поток недоступен';
    case 'playing':
      return 'Локально';
    default:
      return canPlay ? 'Локально' : 'Недоступно';
  }
}

/**
 * Нижняя панель: текущий трек / музыка и локальное прослушивание / глобальные
 * действия. Shuffle и Repeat управляют постоянными настройками программного
 * плейлиста, а не разовой очередью.
 */
export function PlayerBar() {
  const { now, control, reload, reloadControl } = useLive();
  const audio = useAudio();
  const toast = useToast();
  const settings = usePlaylistSettings();
  const [busy, setBusy] = useState<string | null>(null);
  const [restartOpen, setRestartOpen] = useState(false);

  const station = now?.station ?? null;
  const track = now?.track ?? null;
  const desired = station?.desired ?? 'stopped';
  const state = effectiveControl(control, now);
  const statusView = describeStationStatus(station?.status, station?.reason);

  const programId =
    state?.programPlaylistId ?? state?.selectedPlaylistId ?? null;
  const program = settings.get(programId);
  const repeatMode: RepeatMode = program?.repeatMode ?? 'none';
  const shuffle = program?.shuffle ?? false;
  const repeatGovernedBy = program?.repeatGovernedBy ?? state?.repeatGovernedBy ?? null;
  const repeatDisabledReason =
    repeatGovernedBy === 'rotation'
      ? 'Повтор определяется ротацией'
      : !state
        ? 'Состояние источника ещё не получено'
        : !programId
          ? 'Источник эфира не определён'
          : !program
            ? 'Загрузка настроек плейлиста…'
            : null;
  const shuffleDisabledReason = !state
    ? 'Состояние источника ещё не получено'
    : !programId
      ? 'Источник эфира не определён'
      : !program
        ? 'Загрузка настроек плейлиста…'
        : null;

  const lastAudioError = useRef<string | null>(null);
  useEffect(() => {
    if (audio.error && audio.error !== lastAudioError.current) {
      const isStatus = audio.state === 'reconnecting' || audio.state === 'buffering';
      if (!isStatus) toast.notify('error', audio.error);
      lastAudioError.current = audio.error;
    }
    if (!audio.error) lastAudioError.current = null;
  }, [audio.error, audio.state, toast]);

  const run = async (key: string, action: () => Promise<void>, message: string) => {
    setBusy(key);
    try {
      await action();
      if (message) toast.notify('success', message);
      reload();
      reloadControl();
      notifyDataChanged();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const toggleShuffle = async () => {
    if (!programId) return;
    const current = settings.get(programId)?.shuffle ?? false;
    await settings.setShuffle(programId, !current);
  };

  const cycleRepeat = async () => {
    if (!programId || !program) return;
    await settings.cycleRepeat(programId);
  };

  const localBusy = audio.state === 'connecting' || audio.state === 'reconnecting';
  const localFailure = audio.state === 'unavailable';
  const canPressLocal = audio.canPlay || audio.state === 'station_off';
  const localTitle =
    !audio.canPlay && audio.intent === 'idle'
      ? audio.unavailableReason ?? 'Локальное прослушивание недоступно'
      : (audio.state === 'station_off' || audio.state === 'buffering') && audio.intent !== 'idle'
        ? 'Остановить локальное прослушивание'
        : audio.state === 'blocked'
      ? 'Нажмите Play, чтобы продолжить'
      : localBusy
        ? 'Отменить подключение'
        : localFailure
          ? 'Переподключиться к потоку'
          : audio.playing
            ? 'Остановить локальное прослушивание'
            : 'Слушать эфир локально (эфир не меняется)';

  const localClick = () => {
    if (localBusy) {
      audio.cancel();
      return;
    }
    if (localFailure || audio.state === 'blocked') {
      audio.reconnect();
      return;
    }
    void audio.toggle();
  };

  const airDisabled = busy !== null;
  const playTargetName = program?.name ?? null;

  return (
    <footer className="player-bar" aria-label="Панель прослушивания и управления эфиром">
      <div className="pb-track">
        <Link to="/admin" className="pb-track__cover" aria-label="Обзор станции">
          <Cover src={track?.coverUrl} alt={track ? `Обложка: ${track.title}` : 'Нет текущего трека'} size="sm" />
        </Link>
        <div className="pb-track__text">
          <span className="pb-track__title" title={track?.title ?? undefined}>
            {track?.title ?? statusView.label}
          </span>
          <span className="pb-track__artist" title={track?.artist ?? statusView.detail}>
            {track?.artist ?? statusView.shortLabel}
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
            label={
              shuffleDisabledReason ??
              (shuffle
                ? `Выключить перемешивание${playTargetName ? ` «${playTargetName}»` : ''}`
                : `Включить перемешивание${playTargetName ? ` «${playTargetName}»` : ''}`)
            }
            title={
              shuffleDisabledReason ??
              (shuffle
                ? `Выключить перемешивание${playTargetName ? ` «${playTargetName}»` : ''}`
                : `Включить перемешивание${playTargetName ? ` «${playTargetName}»` : ''}`)
            }
            active={shuffle && Boolean(program)}
            disabled={Boolean(shuffleDisabledReason) || airDisabled}
            loading={Boolean(programId && settings.busy(programId, 'shuffle'))}
            onClick={() => void toggleShuffle()}
          />
          <IconButton
            icon={repeatMode === 'one' ? Repeat1 : Repeat}
            label={repeatDisabledReason ?? repeatTooltip(repeatMode, playTargetName)}
            title={repeatDisabledReason ?? repeatTooltip(repeatMode, playTargetName)}
            active={repeatMode !== 'none' && Boolean(program) && repeatGovernedBy !== 'rotation'}
            disabled={Boolean(repeatDisabledReason) || airDisabled}
            loading={Boolean(programId && settings.busy(programId, 'repeat'))}
            onClick={() => void cycleRepeat()}
          />
          <IconButton
            icon={SkipForward}
            label={desired === 'playing' ? 'Пропустить трек для всех слушателей' : 'Сначала запустите эфир'}
            title={desired === 'playing' ? 'Пропустить текущий трек для всех слушателей' : 'Сначала запустите эфир'}
            disabled={desired !== 'playing' || airDisabled}
            onClick={() => void run('skip', api.admin.station.skip, 'Пропуск передан движку')}
          />
        </div>

        <div className="pb-local">
          <IconButton
            icon={
              localBusy
                ? X
                : audio.state === 'station_off' && audio.intent !== 'idle'
                  ? Square
                  : localFailure
                    ? RotateCw
                    : audio.playing || audio.state === 'buffering'
                      ? Pause
                      : Play
            }
            label={localTitle}
            title={localTitle}
            tone="accent"
            active={audio.playing}
            disabled={!canPressLocal && !audio.playing && audio.state !== 'station_off'}
            onClick={localClick}
          />
          <span className="pb-local__label">{localLabel(audio, audio.canPlay)}</span>
          <VolumeControl
            className="pb-volume"
            value={audio.volume}
            onChange={audio.setVolume}
            disabled={!audio.canPlay}
          />
        </div>
      </div>

      <div className="pb-air">
        <span className="pb-air__label" title={statusView.detail} aria-live="polite">
          <span className={`dot ${statusDotClass(statusView.tone)}`} aria-hidden="true" />
          <span className="pb-air__text">{statusView.shortLabel}</span>
        </span>
        <div className="pb-air__buttons">
          <IconButton
            icon={Power}
            label={desired === 'playing' ? 'Эфир уже запущен для всех слушателей' : 'Запустить эфир для всех слушателей'}
            title={desired === 'playing' ? `Эфир уже запущен. ${statusView.detail}` : 'Запустить эфир для всех слушателей'}
            tone="accent"
            active={desired === 'playing'}
            disabled={desired === 'playing' || airDisabled}
            onClick={() => void run('start', api.admin.station.start, 'Эфир запускается')}
          />
          <IconButton
            icon={Square}
            label={desired === 'stopped' ? 'Эфир уже остановлен' : 'Остановить эфир для всех слушателей'}
            title={desired === 'stopped' ? 'Эфир уже остановлен' : 'Остановить эфир для всех слушателей'}
            disabled={desired === 'stopped' || airDisabled}
            onClick={() => void run('stop', api.admin.station.stop, 'Эфир останавливается')}
          />
          <IconButton
            icon={RotateCw}
            label={desired === 'stopped' ? 'Сначала запустите эфир' : 'Перезапустить движок для всех слушателей'}
            title={
              desired === 'stopped'
                ? 'Сначала запустите эфир'
                : 'Перезапустить движок для всех слушателей. Эфир прервётся на несколько секунд.'
            }
            disabled={desired === 'stopped' || airDisabled}
            onClick={() => setRestartOpen(true)}
          />
        </div>
      </div>

      <ConfirmDialog
        open={restartOpen}
        title="Перезапустить движок?"
        description="Эфир прервётся на несколько секунд для всех слушателей. Локальное прослушивание в этом браузере переподключится автоматически."
        confirmLabel="Перезапустить"
        busy={busy === 'restart'}
        onConfirm={() => {
          setRestartOpen(false);
          void run('restart', api.admin.station.restart, 'Движок перезапускается');
        }}
        onCancel={() => setRestartOpen(false)}
      />
    </footer>
  );
}
