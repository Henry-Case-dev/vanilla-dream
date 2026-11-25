import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState } from 'react';
import type { ReactNode } from 'react';
import { useLive } from './live';
import {
  PlaybackController,
  type PlaybackAdapter,
  type PlaybackIntent,
  type PlaybackSnapshot,
  type PlaybackState,
} from './player-controller';

const VOLUME_KEY = 'vd-radio-volume';

export type LocalPlaybackState = PlaybackState;

export interface AudioApi {
  /** Локальное прослушивание потока — не влияет на эфир и очередь станции. */
  playing: boolean;
  busy: boolean;
  volume: number;
  error: string | null;
  canPlay: boolean;
  unavailableReason: string | null;
  state: LocalPlaybackState;
  intent: PlaybackIntent;
  play: () => Promise<void>;
  stop: () => void;
  cancel: () => void;
  reconnect: () => void;
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

const IDLE_SNAPSHOT: PlaybackSnapshot = {
  intent: 'idle',
  state: 'idle',
  error: null,
  attempts: 0,
};

export function AudioProvider({ children }: { children: ReactNode }) {
  const { now } = useLive();
  const streamUrl = now?.station.streamUrl ?? '';
  const desired = now?.station.desired ?? 'stopped';
  const status = now?.station.status ?? 'unknown';
  const unavailableReason = !streamUrl
    ? 'Ссылка на поток ещё не получена от сервера'
    : desired !== 'playing'
      ? 'Сначала запустите эфир'
      : null;

  const audioRef = useRef<HTMLAudioElement | null>(null);
  const [snapshot, setSnapshot] = useState<PlaybackSnapshot>(IDLE_SNAPSHOT);
  const [volume, setVolumeState] = useState(readVolume);
  const volumeRef = useRef(volume);
  const streamRef = useRef(streamUrl);
  const desiredRef = useRef(desired);
  const statusRef = useRef(status);
  const controllerRef = useRef<PlaybackController | null>(null);

  volumeRef.current = volume;
  streamRef.current = streamUrl;
  desiredRef.current = desired;
  statusRef.current = status;

  useEffect(() => {
    const element = audioRef.current;
    if (element) element.volume = volume;
    try {
      window.localStorage.setItem(VOLUME_KEY, String(volume));
    } catch {
      /* приватный режим */
    }
  }, [volume]);

  useEffect(() => {
    const element = audioRef.current;
    if (!element) return;
    const adapter: PlaybackAdapter = {
      setSource: (url) => {
        element.src = url;
        element.load();
      },
      play: () => element.play(),
      pause: () => element.pause(),
      reset: () => {
        element.pause();
        element.removeAttribute('src');
        element.load();
      },
      isPlaying: () => !element.paused && !element.ended && element.readyState >= 2,
      currentTime: () => element.currentTime,
    };
    const controller = new PlaybackController({
      adapter,
      getStation: () => ({
        desired: desiredRef.current,
        status: statusRef.current,
        streamUrl: streamRef.current,
      }),
      onChange: setSnapshot,
    });
    controllerRef.current = controller;
    controller.setStation({
      desired: desiredRef.current,
      status: statusRef.current,
      streamUrl: streamRef.current,
    });
    controller.start();

    const onOnline = () => controller.accelerate();
    window.addEventListener('online', onOnline);

    return () => {
      window.removeEventListener('online', onOnline);
      controller.destroy();
      controllerRef.current = null;
    };
  }, []);

  useEffect(() => {
    controllerRef.current?.setStation({
      desired,
      status,
      streamUrl,
    });
  }, [desired, status, streamUrl]);

  const play = useCallback(async () => {
    controllerRef.current?.play();
  }, []);

  const stop = useCallback(() => {
    controllerRef.current?.stop();
  }, []);

  const cancel = useCallback(() => {
    controllerRef.current?.cancel();
  }, []);

  const reconnect = useCallback(() => {
    controllerRef.current?.reconnect();
  }, []);

  const toggle = useCallback(async () => {
    if (snapshot.state === 'blocked' || snapshot.state === 'unavailable') {
      reconnect();
      return;
    }
    if (snapshot.intent !== 'idle') {
      stop();
      return;
    }
    await play();
  }, [snapshot.intent, snapshot.state, play, reconnect, stop]);

  const setVolume = useCallback((value: number) => {
    const clamped = Math.min(1, Math.max(0, value));
    setVolumeState(clamped);
  }, []);

  const value = useMemo<AudioApi>(
    () => ({
      playing: snapshot.state === 'playing',
      busy: snapshot.state === 'connecting' || snapshot.state === 'reconnecting',
      volume,
      error: snapshot.error,
      canPlay: Boolean(streamUrl) && desired === 'playing',
      unavailableReason,
      state: snapshot.state,
      intent: snapshot.intent,
      play,
      stop,
      cancel,
      reconnect,
      toggle,
      setVolume,
    }),
    [
      snapshot.state,
      snapshot.error,
      snapshot.intent,
      volume,
      streamUrl,
      desired,
      unavailableReason,
      play,
      stop,
      cancel,
      reconnect,
      toggle,
      setVolume,
    ],
  );

  return (
    <AudioContext.Provider value={value}>
      {children}
      <audio
        ref={audioRef}
        data-local-audio="1"
        preload="none"
        onPlaying={() => controllerRef.current?.notify('playing')}
        onPause={() => controllerRef.current?.notify('pause')}
        onEnded={() => controllerRef.current?.notify('ended')}
        onWaiting={() => controllerRef.current?.notify('waiting')}
        onStalled={() => controllerRef.current?.notify('stalled')}
        onError={() => controllerRef.current?.notify('error')}
        onAbort={() => controllerRef.current?.notify('abort')}
        onEmptied={() => controllerRef.current?.notify('emptied')}
        onTimeUpdate={() => controllerRef.current?.notify('timeupdate')}
      />
    </AudioContext.Provider>
  );
}

export function useAudio(): AudioApi {
  const context = useContext(AudioContext);
  if (!context) throw new Error('AudioProvider отсутствует в дереве компонентов');
  return context;
}
