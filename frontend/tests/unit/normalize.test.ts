import { describe, expect, it } from 'vitest';
import {
  PayloadError,
  normalizeAddTracksResult,
  normalizeLinks,
  normalizePlaylist,
  normalizePlaylistDetail,
  normalizeQueueSnapshot,
  normalizeServices,
  normalizeSettings,
  normalizeStorage,
  normalizeTrackIds,
} from '../../src/lib/api';
import { describeStationStatus } from '../../src/lib/status';
import { scopeSelectionState, toggleScopeSelection } from '../../src/lib/selection';

const trackPayload = {
  id: 't1',
  title: 'Сиреневый дождь',
  artist: 'Vanilla Dream',
  album: 'Ночные волны',
  genre: 'Ambient',
  year: 2026,
  duration: 238.0,
  bitrate: 320,
  size: 9520000,
  ext: 'mp3',
  cover_url: 'https://radio.example/api/public/cover/t1',
  play_count: 12,
  pending_delete: false,
  created_at: '2026-11-20T18:00:00+00:00',
  updated_at: '2026-11-20T18:00:00+00:00',
  stream_url: '/api/admin/tracks/t1/stream',
};

/** Форма ответа GET /api/admin/playlists/{id} по contract-1.0.1 §B. */
const playlistDetailPayload = {
  playlist: {
    id: 'p1',
    name: 'Ночной сиреневый',
    description: 'Медленные треки',
    system: false,
    shuffle: true,
    repeat_mode: 'all',
    cover_url: 'https://radio.example/api/public/playlist-cover/p1?v=1764000000',
    cover_kind: 'auto',
    track_count: 1,
    duration: 238.0,
    created_at: '2026-11-20T18:00:00+00:00',
    updated_at: '2026-11-20T18:05:00+00:00',
    tracks: [trackPayload],
    tracks_total: 1,
  },
};

describe('playlist detail normalizer', () => {
  it('reads tracks from playlist.tracks (prod defect: empty playlist)', () => {
    const detail = normalizePlaylistDetail(playlistDetailPayload);
    expect(detail.playlist.id).toBe('p1');
    expect(detail.playlist.trackCount).toBe(1);
    expect(detail.playlist.duration).toBe(238);
    expect(detail.tracks.map((track) => track.id)).toEqual(['t1']);
    expect(detail.tracksTotal).toBe(1);
  });

  it('does not silently turn a malformed detail into an empty playlist', () => {
    expect(() => normalizePlaylistDetail({ playlist: { id: 'p1' } })).toThrow(PayloadError);
    expect(() =>
      normalizePlaylistDetail({ playlist: { ...playlistDetailPayload.playlist, tracks: 'nope' } }),
    ).toThrow(PayloadError);
  });

  it('normalizes the system playlist flags and fixed name', () => {
    const system = normalizePlaylist({
      id: 'library',
      name: 'library',
      description: 'Все треки медиатеки. Обновляется автоматически.',
      system: true,
      shuffle: true,
      repeat_mode: 'all',
      cover_url: null,
      cover_kind: 'none',
      track_count: 42,
      duration: 7200,
      created_at: null,
      updated_at: null,
    });
    expect(system.system).toBe(true);
    expect(system.name).toBe('Вся медиатека');
    expect(system.trackCount).toBe(42);
  });
});

describe('services normalizer', () => {
  const servicesPayload = {
    services: [
      {
        id: 'api',
        unit: 'vanilla-dream-radio-api.service',
        label: 'Backend',
        state: 'active',
        state_label: 'Работает',
        detail: 'Сервис отвечает',
        last_start: '2026-11-25T01:00:00+00:00',
        last_error: null,
      },
      {
        id: 'liquidsoap',
        unit: 'vanilla-dream-radio-liquidsoap.service',
        label: 'Liquidsoap',
        state: 'active',
        state_label: 'Работает',
        detail: 'systemctl is-active',
        last_start: null,
        last_error: null,
      },
    ],
    last_start: '2026-11-25T04:07:00+00:00',
    last_error: 'Движок перезапускался',
  };

  it('reads services[].unit and top-level engine fallbacks', () => {
    const services = normalizeServices(servicesPayload);
    expect(services).toHaveLength(2);
    expect(services[0]?.unit).toBe('vanilla-dream-radio-api.service');
    expect(services[1]?.label).toBe('Liquidsoap');
    expect(services[1]?.lastStart).toBe('2026-11-25T04:07:00+00:00');
    expect(services[1]?.lastError).toBe('Движок перезапускался');
  });

  it('throws instead of showing «Нет данных о службах» for a malformed payload', () => {
    expect(() => normalizeServices({})).toThrow(PayloadError);
  });
});

describe('storage normalizer', () => {
  it('reads numeric contract fields, not object-shaped guesses', () => {
    const storage = normalizeStorage({
      tracks: 7,
      bytes: 74040000,
      cover_bytes: 120000,
      db_bytes: 65536,
      free_bytes: 41000000000,
      total_bytes: 97000000000,
      limit_gb: 0,
    });
    expect(storage).toEqual({
      tracks: 7,
      bytes: 74040000,
      coverBytes: 120000,
      dbBytes: 65536,
      freeBytes: 41000000000,
      totalBytes: 97000000000,
      limitGb: 0,
    });
  });

  it('rejects malformed numeric storage instead of showing zeroes', () => {
    expect(() => normalizeStorage({ tracks: '7', bytes: 1, free_bytes: 2, total_bytes: 3 })).toThrow(
      PayloadError,
    );
  });
});

describe('queue normalizer', () => {
  const queuePayload = {
    current: {
      position_id: 'pos-current',
      track: trackPayload,
      source: { kind: 'library', label: 'Вся медиатека', playlist_id: 'library', name: 'Вся медиатека' },
      started_at: '2026-11-25T04:00:00+00:00',
      elapsed: 12.5,
    },
    items: [
      {
        id: 'pos-next',
        track: { ...trackPayload, id: 't2', title: 'Тихий сигнал' },
        source: { kind: 'library', label: 'Вся медиатека', playlist_id: 'library', name: 'Вся медиатека' },
        prepared: true,
        manual: false,
        estimated_start: '2026-11-25T04:04:00+00:00',
      },
    ],
    depth: 6,
  };

  it('accepts queue.current.position_id from the backend snapshot', () => {
    const snapshot = normalizeQueueSnapshot(queuePayload);
    expect(snapshot.current?.id).toBe('pos-current');
    expect(snapshot.items[0]?.id).toBe('pos-next');
    expect(snapshot.items[0]?.prepared).toBe(true);
  });

  it('throws for an item without id/position_id', () => {
    expect(() =>
      normalizeQueueSnapshot({ current: { track: trackPayload }, items: [], depth: 6 }),
    ).toThrow(PayloadError);
  });
});

describe('bulk add and ids', () => {
  it('normalizes the documented add result', () => {
    const result = normalizeAddTracksResult({
      added: 3,
      skipped: 1,
      unavailable: 2,
      playlist: {
        ...playlistDetailPayload.playlist,
        tracks: undefined,
        tracks_total: undefined,
        track_count: 4,
      },
    });
    expect(result.added).toBe(3);
    expect(result.skipped).toBe(1);
    expect(result.unavailable).toBe(2);
    expect(result.playlist.trackCount).toBe(4);
  });

  it('deduplicates ids and validates total', () => {
    expect(normalizeTrackIds({ ids: ['a', 'b', 'a'], total: 3 })).toEqual({
      ids: ['a', 'b'],
      total: 3,
    });
    expect(() => normalizeTrackIds({ ids: ['a'] })).toThrow(PayloadError);
  });
});

describe('links normalizer', () => {
  const linksPayload = {
    links: [
      {
        key: 'mp3',
        label: 'Прямой MP3',
        url: 'https://radio.example/radio.mp3',
        description: 'Прямая ссылка на аудиопоток',
        primary: true,
      },
      {
        key: 'metadata',
        label: 'Метаданные эфира',
        url: 'https://radio.example/api/public/now-playing',
        description: 'JSON с текущим треком',
      },
    ],
  };

  it('renders exactly the server link list and keeps mp3 primary', () => {
    const links = normalizeLinks(linksPayload);
    expect(links.map((link) => link.key)).toEqual(['mp3', 'metadata']);
    expect(links[0]?.primary).toBe(true);
    expect(links.some((link) => link.key === 'world_radio')).toBe(false);
    expect(links[1]?.label).toBe('Метаданные эфира');
  });

  it('throws for the old keyed-object payload instead of inventing Minecraft links', () => {
    expect(() => normalizeLinks({ world_radio: 'https://radio.example/radio.mp3' })).toThrow(
      PayloadError,
    );
  });
});

describe('shared station status mapping', () => {
  it('is green only for a real on_air state', () => {
    expect(describeStationStatus('on_air', 'playing').tone).toBe('live');
    expect(describeStationStatus('no_music', 'library_empty').tone).toBe('idle');
    expect(describeStationStatus('error', 'engine_unavailable').tone).toBe('error');
    expect(describeStationStatus('off', 'stopped_by_owner').tone).toBe('idle');
  });

  it('uses different messages for empty library, empty playlist and exhausted playlist', () => {
    const library = describeStationStatus('no_music', 'library_empty');
    const empty = describeStationStatus('no_music', 'playlist_empty');
    const exhausted = describeStationStatus('no_music', 'playlist_exhausted');
    expect(new Set([library.label, empty.label, exhausted.label]).size).toBe(3);
    expect(library.detail).toContain('медиатеке');
    expect(empty.detail).toContain('плейлист');
    expect(exhausted.detail).toContain('повтор');
  });

  it('never reports healthy air for stalled playback', () => {
    const stalled = describeStationStatus('error', 'playback_stalled');
    expect(stalled.isLive).toBe(false);
    expect(stalled.tone).toBe('error');
    expect(stalled.label).toContain('остановился');
  });
});

describe('selection helpers', () => {
  it('keeps selections from other searches while toggling the current scope', () => {
    const first = new Set(['other-query-id']);
    const scope = ['a', 'b'];
    expect(scopeSelectionState(first, scope)).toBe('none');
    const all = toggleScopeSelection(first, scope);
    expect(scopeSelectionState(all, scope)).toBe('all');
    expect(all.has('other-query-id')).toBe(true);
    const cleared = toggleScopeSelection(all, scope);
    expect(scopeSelectionState(cleared, scope)).toBe('none');
    expect(cleared.has('other-query-id')).toBe(true);
  });
});

describe('settings normalizer', () => {
  it('maps the system playlist id to the explicit automatic option', () => {
    const settings = normalizeSettings({
      settings: { default_playlist_id: 'library', bitrate: '128' },
    });
    expect(settings.defaultPlaylistId).toBe('');
  });
});
