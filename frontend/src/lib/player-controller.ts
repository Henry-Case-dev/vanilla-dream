/**
 * Локальный плеер отдельно от эфира станции.
 *
 * Намерение пользователя (intent) живёт само по себе: события браузера
 * (pause/ended/error/abort/emptied) не выключают прослушивание, пока
 * пользователь явно не нажал Stop. Переподключение всегда создаёт новое
 * соединение с тем же URL (cache-busting только на reconnect), повторные
 * попытки идут по 1/2/4/8/15/30 с, а устаревшие ответы отбрасываются по
 * attempt id.
 */

export type PlaybackIntent = 'idle' | 'wants' | 'playing' | 'reconnecting' | 'error';

export type PlaybackState =
  | 'idle'
  | 'connecting'
  | 'playing'
  | 'buffering'
  | 'reconnecting'
  | 'station_off'
  | 'unavailable'
  | 'blocked';

export type AudioEvent =
  | 'playing'
  | 'pause'
  | 'ended'
  | 'waiting'
  | 'stalled'
  | 'error'
  | 'abort'
  | 'emptied'
  | 'timeupdate';

export interface PlaybackSnapshot {
  intent: PlaybackIntent;
  state: PlaybackState;
  error: string | null;
  /** Сколько автоматических переподключений уже запланировано. */
  attempts: number;
}

export interface PlaybackAdapter {
  setSource: (url: string) => void;
  play: () => Promise<void>;
  pause: () => void;
  reset: () => void;
  isPlaying: () => boolean;
  currentTime: () => number;
}

export interface StationSignal {
  desired: 'playing' | 'stopped';
  status: string;
  streamUrl: string;
}

export interface PlaybackControllerOptions {
  adapter: PlaybackAdapter;
  getStation: () => StationSignal;
  onChange?: (snapshot: PlaybackSnapshot) => void;
  now?: () => number;
  setTimeout?: (handler: () => void, delay: number) => number;
  clearTimeout?: (id: number) => void;
  setInterval?: (handler: () => void, delay: number) => number;
  clearInterval?: (id: number) => void;
}

export const RETRY_DELAYS = [1000, 2000, 4000, 8000, 15000, 30000] as const;
export const BUFFER_GRACE_MS = 10_000;
export const SLOW_CHECK_MS = 30_000;
export const BLOCKED_MESSAGE =
  'Браузер остановил воспроизведение. Нажмите Play, чтобы продолжить';
const CONNECTION_ERROR =
  'Не удалось подключиться к потоку. Проверьте соединение и нажмите «Переподключиться».';
const INTENTIONAL_EVENT_WINDOW_MS = 250;

function isNotAllowed(error: unknown): boolean {
  return (
    typeof error === 'object' &&
    error !== null &&
    'name' in error &&
    (error as { name?: unknown }).name === 'NotAllowedError'
  );
}

export class PlaybackController {
  private readonly adapter: PlaybackAdapter;
  private readonly getStation: () => StationSignal;
  private readonly onChange: (snapshot: PlaybackSnapshot) => void;
  private readonly now: () => number;
  private readonly scheduleTimeout: (handler: () => void, delay: number) => number;
  private readonly cancelTimeout: (id: number) => void;
  private readonly scheduleInterval: (handler: () => void, delay: number) => number;
  private readonly cancelInterval: (id: number) => void;

  private snapshot: PlaybackSnapshot = { intent: 'idle', state: 'idle', error: null, attempts: 0 };
  private retryTimer: number | null = null;
  private slowTimer: number | null = null;
  private tickTimer: number | null = null;
  private attemptId = 0;
  private retryIndex = 0;
  private lastProgressAt = 0;
  private lastCurrentTime = -1;
  private intentionalUntil = 0;
  private started = false;
  private lastStation: StationSignal | null = null;

  constructor(options: PlaybackControllerOptions) {
    this.adapter = options.adapter;
    this.getStation = options.getStation;
    this.onChange = options.onChange ?? (() => undefined);
    this.now = options.now ?? (() => Date.now());
    this.scheduleTimeout =
      options.setTimeout ?? ((handler, delay) => window.setTimeout(handler, delay));
    this.cancelTimeout =
      options.clearTimeout ?? ((id) => window.clearTimeout(id));
    this.scheduleInterval =
      options.setInterval ?? ((handler, delay) => window.setInterval(handler, delay));
    this.cancelInterval =
      options.clearInterval ?? ((id) => window.clearInterval(id));
  }

  snapshotValue(): PlaybackSnapshot {
    return this.snapshot;
  }

  private emit(patch: Partial<PlaybackSnapshot>): void {
    this.snapshot = { ...this.snapshot, ...patch };
    this.onChange(this.snapshot);
  }

  private intentional(): void {
    this.intentionalUntil = this.now() + INTENTIONAL_EVENT_WINDOW_MS;
  }

  private wasIntentional(): boolean {
    return this.now() <= this.intentionalUntil;
  }

  private stationAllowsPlay(): boolean {
    const station = this.getStation();
    return station.desired === 'playing' && Boolean(station.streamUrl);
  }

  start(): void {
    if (this.started) return;
    this.started = true;
    this.tickTimer = this.scheduleInterval(() => this.tick(), 1000);
  }

  destroy(): void {
    this.started = false;
    this.attemptId += 1;
    this.clearRetry();
    this.clearSlow();
    if (this.tickTimer !== null) {
      this.cancelInterval(this.tickTimer);
      this.tickTimer = null;
    }
  }

  private clearRetry(): void {
    if (this.retryTimer !== null) {
      this.cancelTimeout(this.retryTimer);
      this.retryTimer = null;
    }
  }

  private clearSlow(): void {
    if (this.slowTimer !== null) {
      this.cancelTimeout(this.slowTimer);
      this.slowTimer = null;
    }
  }

  /** Явный Play: всегда создаёт соединение, если элемент не играет. */
  play(): void {
    this.clearRetry();
    this.clearSlow();
    this.retryIndex = 0;
    if (!this.stationAllowsPlay()) {
      this.emit({
        intent: this.getStation().desired === 'playing' ? 'wants' : 'wants',
        state: this.getStation().desired === 'playing' ? 'unavailable' : 'station_off',
        error: null,
        attempts: 0,
      });
      return;
    }
    if (this.adapter.isPlaying()) {
      this.markPlaying();
      return;
    }
    this.emit({ intent: 'wants', state: 'connecting', error: null, attempts: 0 });
    this.connect(false);
  }

  /** Повторная попытка после длительной ошибки. */
  reconnect(): void {
    this.retryIndex = 0;
    this.clearRetry();
    this.emit({ intent: 'wants', state: 'connecting', error: null, attempts: 0 });
    if (!this.stationAllowsPlay()) {
      this.emit({ state: 'station_off' });
      return;
    }
    this.connect(true);
  }

  /** Отмена текущей попытки/переподключения — это явный локальный Stop. */
  cancel(): void {
    this.stop();
  }

  stop(): void {
    this.attemptId += 1;
    this.clearRetry();
    this.clearSlow();
    this.retryIndex = 0;
    this.intentional();
    this.adapter.pause();
    this.adapter.reset();
    this.emit({ intent: 'idle', state: 'idle', error: null, attempts: 0 });
  }

  /** Новое состояние станции; не трогает трек/очередь/SSE. */
  setStation(next: StationSignal): void {
    const previous = this.lastStation;
    this.lastStation = next;
    const intent = this.snapshot.intent;
    if (intent === 'idle') return;
    if (next.desired === 'stopped') {
      this.attemptId += 1;
      this.clearRetry();
      this.clearSlow();
      this.intentional();
      this.adapter.pause();
      this.emit({
        state: 'station_off',
        error: null,
        attempts: this.retryIndex,
      });
      this.scheduleSlowCheck();
      return;
    }
    if (!next.streamUrl) {
      this.emit({ state: 'unavailable', error: null });
      return;
    }
    const cameOnAir =
      previous !== null &&
      (previous.desired !== 'playing' || previous.status !== next.status) &&
      next.status !== 'off' &&
      next.status !== 'starting';
    if (cameOnAir) {
      this.accelerate();
      return;
    }
    // Станция снова в эфире после остановки владельцем — продолжаем сразу.
    if (previous !== null && previous.desired !== 'playing' && next.desired === 'playing') {
      this.clearSlow();
      this.accelerate();
    }
  }

  /** Ускоряет следующую попытку (online, станция снова в эфире). */
  accelerate(): void {
    const intent = this.snapshot.intent;
    if (intent === 'idle' || this.snapshot.state === 'blocked') return;
    if (!this.stationAllowsPlay()) {
      this.emit({ state: 'station_off' });
      return;
    }
    this.clearRetry();
    this.clearSlow();
    if (this.adapter.isPlaying()) {
      this.markPlaying();
      return;
    }
    this.emit({
      state: this.snapshot.state === 'buffering' ? 'reconnecting' : this.snapshot.state,
    });
    this.connect(true);
  }

  notify(event: AudioEvent): void {
    switch (event) {
      case 'playing':
        if (this.snapshot.intent === 'idle') {
          this.intentional();
          this.adapter.pause();
          return;
        }
        this.markPlaying();
        return;
      case 'timeupdate': {
        const time = this.adapter.currentTime();
        if (Math.abs(time - this.lastCurrentTime) > 0.05) {
          this.lastCurrentTime = time;
          this.lastProgressAt = this.now();
        }
        return;
      }
      case 'waiting':
      case 'stalled':
        if (this.snapshot.intent === 'idle') return;
        if (this.snapshot.intent === 'playing') {
          this.emit({ state: 'buffering', error: null });
          if (this.now() - this.lastProgressAt > BUFFER_GRACE_MS) this.reconnectNow();
        }
        return;
      case 'pause':
        if (this.wasIntentional()) return;
        if (this.snapshot.intent === 'idle') return;
        if (this.getStation().desired === 'stopped') {
          this.emit({ state: 'station_off', error: null });
          return;
        }
        this.fail('Соединение с потоком прервано. Переподключаемся…');
        return;
      case 'ended':
        if (this.wasIntentional() || this.snapshot.intent === 'idle') return;
        this.emit({ state: 'reconnecting', error: null });
        this.retryIndex = 0;
        this.scheduleRetry(1000);
        return;
      case 'error':
      case 'abort':
      case 'emptied':
        if (this.wasIntentional()) return;
        if (this.snapshot.intent === 'idle') return;
        this.fail('Соединение с потоком прервано. Переподключаемся…');
        return;
      default:
        return;
    }
  }

  private markPlaying(): void {
    this.clearRetry();
    this.clearSlow();
    this.retryIndex = 0;
    this.lastProgressAt = this.now();
    this.lastCurrentTime = this.adapter.currentTime();
    this.emit({ intent: 'playing', state: 'playing', error: null, attempts: 0 });
  }

  private fail(message: string): void {
    if (this.snapshot.intent === 'idle' || this.snapshot.state === 'blocked') return;
    this.emit({
      intent: this.snapshot.intent === 'playing' ? 'reconnecting' : this.snapshot.intent,
      state: 'reconnecting',
      error: message,
    });
    this.scheduleRetry();
  }

  private reconnectNow(): void {
    this.clearRetry();
    this.attemptId += 1;
    this.emit({ intent: 'reconnecting', state: 'reconnecting', error: null });
    this.connect(true);
  }

  private scheduleRetry(delay?: number): void {
    if (this.retryTimer !== null) return;
    if (this.snapshot.intent === 'idle' || this.snapshot.state === 'blocked') return;
    if (!this.stationAllowsPlay()) {
      this.emit({ state: 'station_off' });
      this.scheduleSlowCheck();
      return;
    }
    const wait =
      delay ?? RETRY_DELAYS[Math.min(this.retryIndex, RETRY_DELAYS.length - 1)] ?? 30_000;
    this.retryIndex += 1;
    this.emit({ attempts: this.retryIndex });
    this.retryTimer = this.scheduleTimeout(() => {
      this.retryTimer = null;
      if (this.snapshot.intent === 'idle') return;
      if (!this.stationAllowsPlay()) {
        this.emit({ state: 'station_off' });
        this.scheduleSlowCheck();
        return;
      }
      this.connect(true);
    }, wait);
  }

  private scheduleSlowCheck(): void {
    if (this.slowTimer !== null) return;
    if (this.snapshot.intent === 'idle') return;
    this.slowTimer = this.scheduleTimeout(() => {
      this.slowTimer = null;
      if (this.snapshot.intent === 'idle') return;
      if (this.stationAllowsPlay()) {
        this.retryIndex = 0;
        this.connect(true);
      } else {
        this.scheduleSlowCheck();
      }
    }, SLOW_CHECK_MS);
  }

  private connect(cacheBust: boolean): void {
    const station = this.getStation();
    if (!station.streamUrl || station.desired !== 'playing') return;
    const id = ++this.attemptId;
    this.lastProgressAt = this.now();
    this.lastCurrentTime = -1;
    const url = cacheBust
      ? `${station.streamUrl}${station.streamUrl.includes('?') ? '&' : '?'}t=${this.now()}`
      : station.streamUrl;
    this.intentional();
    this.adapter.setSource(url);
    const attemptState = this.retryIndex > 0 || this.snapshot.intent === 'reconnecting' ? 'reconnecting' : 'connecting';
    this.emit({
      state: attemptState,
      error: attemptState === 'reconnecting' ? this.snapshot.error : null,
    });
    this.adapter
      .play()
      .then(() => {
        if (id !== this.attemptId) return;
        if (this.snapshot.intent === 'idle') {
          this.adapter.pause();
          return;
        }
        // Некоторые браузеры не присылают playing для живого потока сразу.
        if (this.adapter.isPlaying()) this.markPlaying();
      })
      .catch((caught: unknown) => {
        if (id !== this.attemptId) return;
        if (this.snapshot.intent === 'idle') return;
        if (isNotAllowed(caught)) {
          this.clearRetry();
          this.emit({ state: 'blocked', error: BLOCKED_MESSAGE });
          return;
        }
        const exhausted = this.retryIndex >= RETRY_DELAYS.length;
        this.emit({
          intent: this.snapshot.intent === 'playing' ? 'reconnecting' : this.snapshot.intent,
          state: exhausted ? 'unavailable' : 'reconnecting',
          error: exhausted ? CONNECTION_ERROR : 'Соединение с потоком прервано. Переподключаемся…',
        });
        this.scheduleRetry();
      });
  }

  private tick(): void {
    if (this.snapshot.intent === 'idle' || this.snapshot.state === 'blocked') return;
    if (this.snapshot.state !== 'playing' && this.snapshot.state !== 'buffering') return;
    if (this.now() - this.lastProgressAt >= BUFFER_GRACE_MS) {
      this.lastProgressAt = this.now();
      this.reconnectNow();
    }
  }
}
