import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useLive } from './live';

const VOLUME_KEY = 'vd-radio-volume';

export type LocalPlaybackState = 'idle' | 'connecting' | 'playing' | 'reconnecting' | 'error';

export interface AudioApi {
  /** Локальное прослушивание потока — не влияет на эфир и очередь станции. */
  playing: boolean;
  busy: boolean;
  volume: number;
  error: string | null;
  canPlay: boolean;
  unavailableReason: string | null;
  state: LocalPlaybackState;
  play: () => Promise<void>;
  stop: () => void;
  toggle: () => Promise<void>;
  setVolume: (value: number) => void;
}

const AudioContext = createContext<AudioApi | null>(null);

function readVolume(): number {
  try {
    const raw = window.localStorage.getItem(VOLUME_KEY);
    const value = raw === null ? Number.NaN : Number(raw);
    if (Number.isFinite(value) && value >= 0 && value <= 1) return value;
  } catch {
    /* приватный режим */
  }
  return 0.8;
}

export function AudioProvider({ children }: { children: ReactNode }) {
  const { now } = useLive();
  const streamUrl = now?.station.streamUrl ?? '';
  const desired = now?.station.desired ?? 'stopped';
  const unavailableReason = !streamUrl
    ? 'Ссылка на поток ещё не получена от сервера'
    : desired !== 'playing'
      ? 'Сначала запустите эфир'
      : null;

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [playing, setPlaying] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [volume, setVolumeState] = useState(readVolume);
  const [state, setState] = useState<LocalPlaybackState>('idle');

  const playingRef = useRef(false);
  const retryRef = useRef(0);
  const volumeRef = useRef(volume);
  const urlRef = useRef(streamUrl);
  const desiredRef = useRef(desired);

  playingRef.current = playing;
  volumeRef.current = volume;
  urlRef.current = streamUrl;
  desiredRef.current = desired;

  useEffect(() => {
    const element = audioRef.current;
    if (element) element.volume = volume;
    try {
      window.localStorage.setItem(VOLUME_KEY, String(volume));
    } catch {
      /* приватный режим */
    }
  }, [volume]);

  const play = useCallback(async () => {
    const element = audioRef.current;
    const url = urlRef.current;
    if (!element) return;
    if (!url) {
      setError('Ссылка на поток ещё не получена от сервера');
      return;
    }
    setBusy(true);
    try {
      const absolute = new URL(url, window.location.href).toString();
      if (element.src !== absolute) {
        element.src = url;
        element.load();
      }
      element.volume = volumeRef.current;
      await element.play();
      setPlaying(true);
      setState('playing');
      setError(null);
    } catch (caught) {
      setPlaying(false);
      setState('error');
      if (caught instanceof DOMException && caught.name === 'NotAllowedError') {
        setError('Браузер заблокировал воспроизведение. Нажмите кнопку ещё раз.');
      } else {
        setError('Не удалось подключиться к потоку. Проверьте соединение.');
      }
    } finally {
      setBusy(false);
    }
  }, []);

  const stop = useCallback(() => {
    const element = audioRef.current;
    retryRef.current = 0;
    if (element) {
      element.pause();
      element.removeAttribute('src');
      element.load();
    }
    setPlaying(false);
    setState('idle');
    setError(null);
  }, []);

  const toggle = useCallback(async () => {
    if (playingRef.current) {
      stop();
      return;
    }
    await play();
  }, [play, stop]);

  useEffect(() => {
    if (desired === 'playing') return;
    if (!playingRef.current) return;
    stop();
  }, [desired, stop]);

  const setVolume = useCallback((value: number) => {
    const clamped = Math.min(1, Math.max(0, value));
    setVolumeState(clamped);
  }, []);

  const handlePlaying = useCallback(() => {
    retryRef.current = 0;
    setPlaying(true);
    setState('playing');
    setError(null);
  }, []);

  const handlePause = useCallback(() => {
    setPlaying(false);
    setState('idle');
  }, []);

  const handleError = useCallback(() => {
    if (!playingRef.current) return;
    if (retryRef.current >= 2) {
      setPlaying(false);
      setError('Поток недоступен. Проверьте соединение и попробуйте снова.');
      return;
    }
    retryRef.current += 1;
    setError('Соединение с потоком прервано, переподключение…');
    window.setTimeout(() => {
      const element = audioRef.current;
      const url = urlRef.current;
      if (!element || !url || !playingRef.current) return;
      const separator = url.includes('?') ? '&' : '?';
      element.src = `${url}${separator}t=${Date.now()}`;
      element.load();
      void element.play().catch(() => undefined);
    }, 3000);
  }, []);

  const value = useMemo<AudioApi>(
    () => ({
      playing,
      busy,
      volume,
      error,
      canPlay: Boolean(streamUrl) && desired === 'playing',
      unavailableReason,
      state,
      play,
      stop,
      toggle,
      setVolume,
    }),
    [playing, busy, volume, error, streamUrl, desired, unavailableReason, state, play, stop, toggle, setVolume],
  );

  return (
    <AudioContext.Provider value={value}>
      {children}
      <audio
        ref={audioRef}
        preload="none"
        onPlaying={handlePlaying}
        onPause={handlePause}
        onEnded={handlePause}
        onError={handleError}
      />
    </AudioContext.Provider>
  );
}

export function useAudio(): AudioApi {
  const context = useContext(AudioContext);
  if (!context) throw new Error('AudioProvider отсутствует в дереве компонентов');
  return context;
}
