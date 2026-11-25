import { describe, expect, it } from 'vitest';
import {
  BLOCKED_MESSAGE,
  BUFFER_GRACE_MS,
  PlaybackController,
  type PlaybackAdapter,
  type PlaybackSnapshot,
  type StationSignal,
} from '../../src/lib/player-controller';

class Clock {
  now = 0;
  private seq = 1;
  private timeouts: Array<{ id: number; at: number; fn: () => void }> = [];
  private intervals: Array<{ id: number; every: number; at: number; fn: () => void }> = [];

  setTimeout = (fn: () => void, delay: number): number => {
    const id = this.seq++;
    this.timeouts.push({ id, at: this.now + delay, fn });
    return id;
  };

  clearTimeout = (id: number): void => {
    this.timeouts = this.timeouts.filter((timer) => timer.id !== id);
  };

  setInterval = (fn: () => void, delay: number): number => {
    const id = this.seq++;
    this.intervals.push({ id, every: delay, at: this.now + delay, fn });
    return id;
  };

  clearInterval = (id: number): void => {
    this.intervals = this.intervals.filter((timer) => timer.id !== id);
  };

  pendingTimeouts(): number {
    return this.timeouts.length;
  }

  advance(ms: number): void {
    const target = this.now + ms;
    for (;;) {
      const nextTimeout = this.timeouts.reduce<{ id: number; at: number; fn: () => void } | null>(
        (best, timer) => (timer.at <= target && (!best || timer.at < best.at) ? timer : best),
        null,
      );
      const nextInterval = this.intervals.reduce<{
        id: number;
        every: number;
        at: number;
        fn: () => void;
      } | null>((best, timer) => (timer.at <= target && (!best || timer.at < best.at) ? timer : best), null);
      if (!nextTimeout && !nextInterval) break;
      if (nextTimeout && (!nextInterval || nextTimeout.at <= nextInterval.at)) {
        this.now = nextTimeout.at;
        this.timeouts = this.timeouts.filter((timer) => timer.id !== nextTimeout.id);
        nextTimeout.fn();
      } else if (nextInterval) {
        this.now = nextInterval.at;
        nextInterval.at += nextInterval.every;
        nextInterval.fn();
      }
    }
    this.now = target;
  }
}

class FakeAdapter implements PlaybackAdapter {
  sources: string[] = [];
  playCalls = 0;
  pauseCalls = 0;
  resetCalls = 0;
  playing = false;
  time = 0;
  playImpl: () => Promise<void> = () => {
    this.playing = true;
    return Promise.resolve();
  };

  setSource = (url: string): void => {
    this.sources.push(url);
    this.playing = false;
  };

  play = (): Promise<void> => {
    this.playCalls += 1;
    return this.playImpl();
  };

  pause = (): void => {
    this.pauseCalls += 1;
    this.playing = false;
  };

  reset = (): void => {
    this.resetCalls += 1;
    this.playing = false;
  };

  isPlaying = (): boolean => this.playing;

  currentTime = (): number => this.time;
}

function station(overrides: Partial<StationSignal> = {}): StationSignal {
  return { desired: 'playing', status: 'on_air', streamUrl: '/radio.mp3', ...overrides };
}

function setup(initial: StationSignal = station()) {
  const clock = new Clock();
  const adapter = new FakeAdapter();
  let current = initial;
  let snapshot: PlaybackSnapshot = { intent: 'idle', state: 'idle', error: null, attempts: 0 };
  const controller = new PlaybackController({
    adapter,
    getStation: () => current,
    onChange: (next) => {
      snapshot = next;
    },
    now: () => clock.now,
    setTimeout: clock.setTimeout,
    clearTimeout: clock.clearTimeout,
    setInterval: clock.setInterval,
    clearInterval: clock.clearInterval,
  });
  controller.start();
  controller.setStation(initial);
  return {
    clock,
    adapter,
    controller,
    get snapshot() {
      return snapshot;
    },
    setStation(next: StationSignal) {
      current = next;
      controller.setStation(next);
    },
  };
}

async function flush(): Promise<void> {
  await Promise.resolve();
  await Promise.resolve();
}

describe('local player intent', () => {
  it('keeps user intent when the browser pauses, ends or errors', async () => {
    const ctx = setup();
    ctx.controller.play();
    await flush();
    ctx.adapter.playing = true;
    ctx.controller.notify('playing');
    expect(ctx.snapshot.state).toBe('playing');
    expect(ctx.snapshot.intent).toBe('playing');

    ctx.clock.advance(500);
    ctx.controller.notify('pause');
    expect(ctx.snapshot.intent).toBe('reconnecting');
    expect(ctx.snapshot.state).toBe('reconnecting');
    expect(ctx.adapter.resetCalls).toBe(0);

    ctx.controller.notify('ended');
    expect(ctx.snapshot.intent).not.toBe('idle');
    ctx.controller.notify('error');
    expect(ctx.snapshot.intent).not.toBe('idle');

    ctx.controller.stop();
    expect(ctx.snapshot.intent).toBe('idle');
    expect(ctx.snapshot.state).toBe('idle');
  });

  it('re-creates the same URL on repeated play when the element is not playing', async () => {
    const ctx = setup();
    ctx.controller.play();
    await flush();
    ctx.adapter.playing = true;
    ctx.controller.notify('playing');
    expect(ctx.adapter.sources).toHaveLength(1);

    ctx.controller.play();
    expect(ctx.adapter.sources).toHaveLength(1);

    ctx.adapter.playing = false;
    ctx.controller.play();
    expect(ctx.adapter.sources).toHaveLength(2);
    expect(ctx.adapter.sources[1]).toBe('/radio.mp3');
  });

  it('reconnects after an error on the same URL with cache-busting only on reconnect', async () => {
    const ctx = setup();
    ctx.adapter.playImpl = () => Promise.reject(new Error('stream down'));
    ctx.controller.play();
    await flush();
    expect(ctx.adapter.sources[0]).toBe('/radio.mp3');
    expect(ctx.snapshot.state).toBe('reconnecting');

    ctx.clock.advance(999);
    expect(ctx.adapter.sources).toHaveLength(1);
    ctx.clock.advance(1);
    expect(ctx.adapter.sources).toHaveLength(2);
    expect(ctx.adapter.sources[1]).toMatch(/^\/radio\.mp3\?t=\d+$/);
  });

  it('treats an unexpected ended as a reconnect, not as a stop', async () => {
    const ctx = setup();
    ctx.controller.play();
    await flush();
    ctx.adapter.playing = true;
    ctx.controller.notify('playing');
    ctx.clock.advance(300);
    ctx.controller.notify('ended');
    expect(ctx.snapshot.intent).toBe('playing');
    expect(ctx.snapshot.state).toBe('reconnecting');
    ctx.clock.advance(1000);
    expect(ctx.adapter.sources).toHaveLength(2);
  });

  it('buffers one waiting and reconnects only after the grace window', async () => {
    const ctx = setup();
    ctx.controller.play();
    await flush();
    ctx.adapter.playing = true;
    ctx.controller.notify('playing');
    ctx.adapter.time = 1;
    ctx.controller.notify('timeupdate');
    ctx.clock.advance(BUFFER_GRACE_MS / 2);
    ctx.controller.notify('waiting');
    expect(ctx.snapshot.state).toBe('buffering');
    expect(ctx.adapter.sources).toHaveLength(1);

    ctx.clock.advance(BUFFER_GRACE_MS / 2 + 500);
    expect(ctx.snapshot.state).toBe('reconnecting');
    expect(ctx.adapter.sources.length).toBeGreaterThan(1);
  });

  it('blocks on autoplay rejection without aggressive retries', async () => {
    const ctx = setup();
    ctx.adapter.playImpl = () =>
      Promise.reject(Object.assign(new Error('blocked'), { name: 'NotAllowedError' }));
    ctx.controller.play();
    await flush();
    expect(ctx.snapshot.state).toBe('blocked');
    expect(ctx.snapshot.error).toBe(BLOCKED_MESSAGE);
    ctx.clock.advance(60_000);
    expect(ctx.adapter.sources).toHaveLength(1);
  });

  it('ignores a stale attempt result', async () => {
    const ctx = setup();
    const pending: Array<{ resolve: () => void }> = [];
    ctx.adapter.playImpl = () =>
      new Promise<void>((resolve) => {
        pending.push({ resolve });
      });
    ctx.controller.play();
    await flush();
    ctx.controller.play();
    await flush();
    expect(ctx.adapter.sources).toHaveLength(2);

    ctx.adapter.playing = true;
    pending[0]?.resolve();
    await flush();
    expect(ctx.snapshot.state).toBe('connecting');

    pending[1]?.resolve();
    await flush();
    expect(ctx.snapshot.state).toBe('playing');
  });

  it('keeps one scheduled retry and one active attempt', async () => {
    const ctx = setup();
    ctx.adapter.playImpl = () => Promise.reject(new Error('down'));
    ctx.controller.play();
    await flush();
    expect(ctx.clock.pendingTimeouts()).toBe(1);
    ctx.controller.notify('error');
    ctx.controller.notify('abort');
    expect(ctx.clock.pendingTimeouts()).toBe(1);
    ctx.clock.advance(1000);
    expect(ctx.adapter.sources).toHaveLength(2);
  });

  it('cancels while busy and never retries after cancel', async () => {
    const ctx = setup();
    ctx.adapter.playImpl = () => Promise.reject(new Error('down'));
    ctx.controller.play();
    await flush();
    expect(ctx.snapshot.state).toBe('reconnecting');
    ctx.controller.cancel();
    expect(ctx.snapshot.intent).toBe('idle');
    ctx.clock.advance(60_000);
    expect(ctx.adapter.sources).toHaveLength(1);
  });

  it('keeps intent while the owner stops the station and resumes on start', async () => {
    const ctx = setup();
    ctx.controller.play();
    await flush();
    ctx.adapter.playing = true;
    ctx.controller.notify('playing');

    ctx.setStation(station({ desired: 'stopped', status: 'off' }));
    expect(ctx.snapshot.state).toBe('station_off');
    expect(ctx.snapshot.intent).toBe('playing');
    expect(ctx.adapter.pauseCalls).toBeGreaterThan(0);

    ctx.setStation(station({ desired: 'playing', status: 'on_air' }));
    expect(ctx.adapter.sources).toHaveLength(2);
    expect(ctx.snapshot.state).toBe('connecting');
  });

  it('accelerates on online and station on-air without duplicate timers', async () => {
    const ctx = setup();
    ctx.adapter.playImpl = () => Promise.reject(new Error('down'));
    ctx.controller.play();
    await flush();
    ctx.clock.advance(500);
    ctx.controller.accelerate();
    expect(ctx.adapter.sources).toHaveLength(2);
    expect(ctx.clock.pendingTimeouts()).toBe(0);

    ctx.controller.accelerate();
    expect(ctx.adapter.sources).toHaveLength(3);
  });

  it('does not reconnect on unrelated station/SSE updates', async () => {
    const ctx = setup();
    ctx.controller.play();
    await flush();
    ctx.adapter.playing = true;
    ctx.controller.notify('playing');
    const before = ctx.adapter.sources.length;
    for (let index = 0; index < 5; index += 1) {
      ctx.controller.setStation(station());
      ctx.controller.notify('timeupdate');
    }
    expect(ctx.adapter.sources).toHaveLength(before);
  });

  it('backs off 1/2/4/8/15/30 s and recovers after a long outage', async () => {
    const ctx = setup();
    let fail = true;
    ctx.adapter.playImpl = () => (fail ? Promise.reject(new Error('down')) : Promise.resolve());
    ctx.controller.play();
    await flush();
    const delays = [1000, 2000, 4000, 8000, 15000, 30000];
    for (const delay of delays) {
      const before = ctx.adapter.sources.length;
      ctx.clock.advance(delay);
      await flush();
      expect(ctx.adapter.sources.length).toBe(before + 1);
    }
    expect(ctx.snapshot.state).toBe('unavailable');
    fail = false;
    ctx.controller.reconnect();
    await flush();
    ctx.adapter.playing = true;
    ctx.controller.notify('playing');
    expect(ctx.snapshot.state).toBe('playing');
    expect(ctx.snapshot.intent).toBe('playing');
  });

  it('never auto-connects a visitor who did not press play', () => {
    const ctx = setup(station({ desired: 'stopped', status: 'off' }));
    ctx.setStation(station());
    expect(ctx.adapter.playCalls).toBe(0);
    expect(ctx.snapshot.state).toBe('idle');
  });
});
