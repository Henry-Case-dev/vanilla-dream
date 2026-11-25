import http from 'node:http';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const ROOT = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '..');
const DIST = path.join(ROOT, 'dist');
const PORT = Number(process.env.PORT || 4199);
const BASE = `http://127.0.0.1:${PORT}`;
const COVER = '/mock-cover.svg';

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.woff2': 'font/woff2',
  '.png': 'image/png',
};

const coverSvg = `<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 600 600">
<defs><linearGradient id="g" x1="0" y1="0" x2="1" y2="1"><stop offset="0" stop-color="#382a52"/><stop offset="1" stop-color="#141019"/></linearGradient></defs>
<rect width="600" height="600" fill="url(#g)"/>
<g stroke="#bca1f4" stroke-width="3" stroke-linecap="round" opacity="0.75">
<path d="M160 360v-90"/><path d="M220 400V190"/><path d="M280 370V240"/><path d="M340 410V210"/><path d="M400 360v-80"/><path d="M460 340v-50"/>
</g></svg>`;

const names = [
  'Ночной трамвай',
  'Тихий сигнал',
  'Полночь на кухне',
  'Стеклянный берег',
  'Белый шум моря',
  'Сирень в апреле',
  'Сиреневый дождь',
];

const tracks = Array.from({ length: 120 }, (_, index) => {
  const number = index + 1;
  return {
    id: `t${number}`,
    title: names[index] || `Тихий трек ${String(number).padStart(3, '0')}`,
    artist: index % 3 === 0 ? 'Vanilla Dream' : index % 3 === 1 ? 'Lumen' : 'Медленные письма',
    album: index % 2 === 0 ? 'Ночные волны' : 'Северное сияние',
    genre: 'Ambient',
    year: 2026,
    duration: 180 + (index % 7) * 17,
    bitrate: 320,
    size: 8000000 + index * 1000,
    ext: 'mp3',
    cover_url: index % 4 === 0 ? COVER : null,
    cover_kind: index % 4 === 0 ? 'file' : 'none',
    original_cover_url: COVER,
    is_duplicate: index % 10 === 1,
    duplicate_kind: index % 10 === 1 ? 'tags' : null,
    duplicate_of: index % 10 === 1 ? 't1' : null,
    can_revert_cover: index % 4 === 0,
    source_filename: `track-${String(number).padStart(3, '0')}.mp3`,
    play_count: index,
    pending_delete: false,
    created_at: new Date(Date.UTC(2026, 10, 1, 0, index % 60)).toISOString(),
    updated_at: new Date(Date.UTC(2026, 10, 1, 0, index % 60)).toISOString(),
    stream_url: `/api/admin/tracks/t${number}/stream`,
  };
});

const state = {
  desired: 'playing',
  status: 'on_air',
  reason: 'playing',
  mode: 'playlist',
  selectedPlaylistId: 'p1',
  effectivePlaylistId: 'p1',
  programPlaylistId: 'p1',
  pendingSource: null,
  overrideInfo: null,
  notice: null,
  revision: 5,
  appliedRevision: 5,
  queueRevision: 3,
  historyRevision: 2,
  currentTrackId: 't1',
  currentSource: {
    kind: 'playlist',
    playlist_id: 'p1',
    name: 'Ночной сиреневый',
    reason: 'selected_playlist',
  },
  startedAt: '2026-11-26T04:00:00+00:00',
  queue: [
    { id: 'pos-2', trackId: 't2', manual: false, prepared: true },
    { id: 'pos-3', trackId: 't3', manual: false, prepared: true },
    { id: 'pos-4', trackId: 't4', manual: false, prepared: false },
    { id: 'pos-5', trackId: 't5', manual: false, prepared: false },
  ],
  delayMs: 0,
  audioDelayMs: 0,
  pendingSyncOnAdd: false,
  rotationRepeat: false,
  systemSettings: { shuffle: true, repeat_mode: 'all' },
  playlists: {
    p1: {
      id: 'p1',
      name: 'Ночной сиреневый',
      description: 'Медленные треки для ночного эфира',
      shuffle: true,
      repeat_mode: 'all',
      cover_kind: 'auto',
      cover_url: `${COVER}?v=1764000000`,
      membership: new Set(['t1', 't2', 't3', 't5']),
      updated_at: '2026-11-25T04:00:00+00:00',
    },
    p2: {
      id: 'p2',
      name: 'Утренний свет',
      description: 'Тихий старт дня',
      shuffle: false,
      repeat_mode: 'none',
      cover_kind: 'none',
      cover_url: null,
      membership: new Set(),
      updated_at: '2026-11-25T04:00:00+00:00',
    },
    p3: {
      id: 'p3',
      name: 'Ротация вечера',
      description: 'Плейлист под управлением ротации',
      shuffle: false,
      repeat_mode: 'all',
      repeat_governed_by: 'rotation',
      cover_kind: 'auto',
      cover_url: `${COVER}?v=1764000000`,
      membership: new Set(['t4', 't6']),
      updated_at: '2026-11-25T04:00:00+00:00',
    },
  },
  settings: {
    station_name: 'Vanilla Dream Radio',
    station_description: 'Круглосуточный сиреневый эфир',
    station_genre: 'Eclectic',
    bitrate: 128,
    timezone: 'Asia/Magadan',
    schedule_enabled: true,
    default_transition: 'after_track',
    default_playlist_id: '',
    queue_depth: 6,
    max_upload_mb: 300,
    max_library_gb: 0,
    desired_state: 'playing',
    engine_last_start: '2026-11-25T00:00:00+00:00',
    engine_last_error: '',
  },
};

const byId = new Map(tracks.map((track) => [track.id, track]));

const initialTracks = JSON.stringify(tracks);
const initialPlaylists = JSON.stringify(
  Object.fromEntries(
    Object.entries(state.playlists).map(([id, playlist]) => [
      id,
      { ...playlist, membership: [...playlist.membership] },
    ]),
  ),
);

function resetMock() {
  tracks.splice(0, tracks.length, ...JSON.parse(initialTracks));
  byId.clear();
  tracks.forEach((track) => byId.set(track.id, track));
  const playlists = JSON.parse(initialPlaylists);
  state.playlists = Object.fromEntries(
    Object.entries(playlists).map(([id, playlist]) => [
      id,
      { ...playlist, membership: new Set(playlist.membership) },
    ]),
  );
  state.desired = 'playing';
  state.status = 'on_air';
  state.reason = 'playing';
  state.mode = 'playlist';
  state.selectedPlaylistId = 'p1';
  state.effectivePlaylistId = 'p1';
  state.programPlaylistId = 'p1';
  state.pendingSource = null;
  state.overrideInfo = null;
  state.notice = null;
  state.revision = 5;
  state.appliedRevision = 5;
  state.queueRevision = 3;
  state.historyRevision = 2;
  state.currentTrackId = 't1';
  state.currentSource = {
    kind: 'playlist',
    playlist_id: 'p1',
    name: 'Ночной сиреневый',
    reason: 'selected_playlist',
  };
  state.startedAt = '2026-11-26T04:00:00+00:00';
  state.queue = [
    { id: 'pos-2', trackId: 't2', manual: false, prepared: true },
    { id: 'pos-3', trackId: 't3', manual: false, prepared: true },
    { id: 'pos-4', trackId: 't4', manual: false, prepared: false },
    { id: 'pos-5', trackId: 't5', manual: false, prepared: false },
  ];
  state.delayMs = 0;
  state.audioDelayMs = 0;
  state.pendingSyncOnAdd = false;
  state.rotationRepeat = false;
  state.systemSettings = { shuffle: true, repeat_mode: 'all' };
  stateMedia.batches.clear();
  stateMedia.exports.clear();
  stateMedia.seq = 0;
}

const stateMedia = {
  batches: new Map(),
  exports: new Map(),
  seq: 0,
};

function nowIso(offsetMs = 0) {
  return new Date(Date.now() + offsetMs).toISOString();
}

function nextId(prefix) {
  stateMedia.seq += 1;
  return `${prefix}${stateMedia.seq}`;
}

function comparison(track) {
  return {
    id: track.id,
    title: track.title,
    artist: track.artist,
    album: track.album,
    duration: track.duration,
    size: track.size,
    bitrate: track.bitrate,
    cover_url: track.cover_url,
  };
}

function batchItemPayload(item) {
  return {
    id: item.id,
    filename: item.filename,
    size: item.size,
    state: item.state,
    reason: item.reason,
    metadata: item.metadata,
    matches: item.matches,
    decision: item.decision,
    error: item.error,
  };
}

function batchPayload(batch) {
  const counts = { added: 0, pending: 0, skipped: 0, errors: 0 };
  for (const item of batch.items) {
    if (item.state === 'added' || item.state === 'saved') counts.added += 1;
    else if (item.state === 'pending') counts.pending += 1;
    else if (item.state === 'skipped') counts.skipped += 1;
    else if (item.state === 'error') counts.errors += 1;
  }
  const status =
    counts.pending > 0
      ? 'pending'
      : batch.cancelled
        ? 'cancelled'
        : batch.items.some((item) => item.state === 'uploading' || item.state === 'processing')
          ? 'processing'
          : 'done';
  return {
    id: batch.id,
    status,
    total: batch.items.length,
    counts,
    items: batch.items.map(batchItemPayload),
  };
}

function readRaw(req) {
  return new Promise((resolve) => {
    const chunks = [];
    req.on('data', (chunk) => chunks.push(chunk));
    req.on('end', () => resolve(Buffer.concat(chunks)));
  });
}

function multipartField(buffer, name) {
  const text = buffer.toString('latin1');
  const match = text.match(new RegExp(`name="${name}"\\r?\\n\\r?\\n([^\\r]*)`));
  return match ? Buffer.from(match[1], 'latin1').toString('utf8') : '';
}

function multipartFilename(buffer) {
  const text = buffer.toString('latin1');
  const match = text.match(/filename="([^"]*)"/);
  if (!match) return 'upload.mp3';
  return Buffer.from(match[1], 'latin1').toString('utf8');
}

function exportJobPayload(job) {
  return {
    id: job.id,
    state: job.state,
    processed: job.processed,
    total: job.total,
    progress: job.total > 0 ? job.processed / job.total : 0,
    archive: job.archive,
    error: job.error,
    expires_at: job.expires_at,
  };
}

function playlistTracks(id) {
  if (id === 'library') return tracks;
  const playlist = state.playlists[id];
  if (!playlist) return [];
  return [...playlist.membership].map((trackId) => byId.get(trackId)).filter(Boolean);
}

function userPlaylistCount(trackId) {
  return Object.values(state.playlists).filter((playlist) =>
    playlist.membership.has(trackId),
  ).length;
}

function trackWithCounts(track) {
  return { ...track, playlists_count: userPlaylistCount(track.id) };
}

function playlistPayload(id) {
  if (id === 'library') {
    const list = tracks;
    return {
      id: 'library',
      name: 'Вся медиатека',
      description: 'Все треки медиатеки. Обновляется автоматически.',
      system: true,
      shuffle: state.systemSettings.shuffle,
      repeat_mode: state.systemSettings.repeat_mode,
      repeat_governed_by: state.rotationRepeat
        ? 'rotation'
        : state.programPlaylistId === 'library'
          ? 'playlist'
          : null,
      cover_url: `${COVER}?v=1764000000`,
      cover_kind: 'auto',
      track_count: list.length,
      duration: list.reduce((sum, track) => sum + track.duration, 0),
      created_at: '2026-11-01T00:00:00+00:00',
      updated_at: '2026-11-25T04:00:00+00:00',
      pending_delete_count: 0,
    };
  }
  const playlist = state.playlists[id];
  const list = playlistTracks(id);
  return {
    id,
    name: playlist.name,
    description: playlist.description,
    system: false,
    shuffle: playlist.shuffle,
    repeat_mode: playlist.repeat_mode,
    cover_url: playlist.cover_url,
    cover_kind: playlist.cover_kind,
    track_count: list.length,
    duration: list.reduce((sum, track) => sum + track.duration, 0),
    repeat_governed_by:
      state.rotationRepeat || playlist.repeat_governed_by === 'rotation'
        ? 'rotation'
        : state.programPlaylistId === id
          ? 'playlist'
          : null,
    created_at: '2026-11-01T00:00:00+00:00',
    updated_at: playlist.updated_at,
    pending_delete_count: 0,
  };
}

function detailPayload(id) {
  const list = playlistTracks(id).map(trackWithCounts);
  return {
    playlist: {
      ...playlistPayload(id),
      tracks: list,
      tracks_total: list.length,
    },
  };
}

function playlistRecord(id) {
  if (id === 'library') {
    return {
      id: 'library',
      name: 'Вся медиатека',
      shuffle: state.systemSettings.shuffle,
      repeat_mode: state.systemSettings.repeat_mode,
    };
  }
  return state.playlists[id] ?? null;
}

function programPlaylist() {
  return playlistRecord(state.programPlaylistId);
}

function effectivePlaylist() {
  return playlistRecord(state.effectivePlaylistId);
}

function effectiveSourcePayload() {
  if (state.overrideInfo) {
    return {
      kind: 'override',
      playlist_id: state.overrideInfo.playlist_id,
      name: state.overrideInfo.name,
      reason: 'override',
    };
  }
  if (state.mode === 'auto') {
    return {
      kind: 'rotation',
      playlist_id: state.programPlaylistId,
      name: 'Ротация',
      reason: 'rotation',
    };
  }
  const playlist = effectivePlaylist();
  if (!playlist) return { kind: 'none', playlist_id: null, name: null, reason: null };
  const isLibrary = playlist.id === 'library';
  return {
    kind: isLibrary ? 'library' : 'playlist',
    playlist_id: playlist.id,
    name: playlist.name,
    reason: isLibrary ? 'system_playlist' : 'selected_playlist',
  };
}

function manualSourcePayload() {
  return {
    kind: 'manual',
    label: 'Ручная вставка',
    playlist_id: null,
    name: 'Ручная вставка',
    reason: 'manual',
  };
}

function repeatGovernedBy() {
  if (state.rotationRepeat) return 'rotation';
  const playlist = programPlaylist();
  if (!playlist) return null;
  if (playlist.repeat_governed_by === 'rotation') return 'rotation';
  return 'playlist';
}

function overridePayload() {
  if (!state.overrideInfo) return null;
  return {
    id: state.overrideInfo.id ?? 'ov-1',
    kind: 'playlist',
    playlist_id: state.overrideInfo.playlist_id,
    playlist_name: state.overrideInfo.name,
    scope: state.overrideInfo.scope ?? 'until_schedule_change',
    expires_at: state.overrideInfo.until,
    active: true,
    created_at: state.overrideInfo.requested_at,
    note: state.overrideInfo.note ?? null,
  };
}

function stationStatePayload() {
  const status = state.desired === 'playing' ? state.status : 'off';
  return {
    mode: state.mode,
    selected_playlist_id: state.selectedPlaylistId,
    program_playlist_id: state.programPlaylistId,
    effective_source: effectiveSourcePayload(),
    pending_source: state.pendingSource,
    override: overridePayload(),
    desired: state.desired,
    status,
    reason: state.desired === 'playing' ? state.reason : 'stopped_by_owner',
    repeat_governed_by: repeatGovernedBy(),
    engine_instance: 'mock-engine-1',
    state_revision: state.revision,
    applied_revision: state.appliedRevision,
    notice: state.notice ?? null,
    diagnostic_reason: null,
  };
}

function nowPlaying(req) {
  const stoppedHeader = req.headers['x-mock-stopped'] === '1';
  const desired = stoppedHeader ? 'stopped' : state.desired;
  const playing = desired === 'playing';
  const current = playing ? byId.get(state.currentTrackId) ?? byId.get('t1') : null;
  const status = stoppedHeader ? 'off' : playing ? (state.status === 'on_air' ? 'on_air' : state.status) : 'off';
  const reason = stoppedHeader ? 'stopped_by_owner' : playing ? state.reason : 'stopped_by_owner';
  const effective = effectiveSourcePayload();
  const nextTrack = state.queue[0] ? byId.get(state.queue[0].trackId) : null;
  return {
    station: {
      name: state.settings.station_name,
      description: state.settings.station_description,
      status,
      status_label: status === 'on_air' ? 'В эфире' : status === 'off' ? 'Остановлено' : status,
      reason,
      desired,
      stream_url: `${BASE}/radio.mp3`,
      m3u_url: `${BASE}/radio.m3u`,
      pls_url: `${BASE}/radio.pls`,
      base_url: BASE,
      bitrate: state.settings.bitrate,
      mode: state.mode,
      program_playlist_id: state.programPlaylistId,
      effective_source: effective,
      pending_source: state.pendingSource,
      override: overridePayload(),
      repeat_governed_by: repeatGovernedBy(),
      state_revision: state.revision,
      notice: state.notice ?? null,
      diagnostic_reason: null,
    },
    track: current
      ? {
          id: current.id,
          title: current.title,
          artist: current.artist,
          album: current.album,
          genre: current.genre,
          year: current.year,
          cover_url: current.cover_url,
          duration: current.duration,
          started_at: state.startedAt,
          elapsed: 96,
          progress: 96 / current.duration,
        }
      : null,
    source: playing
      ? state.currentSource ?? effective
      : { kind: 'none', label: 'Тишина', playlist_id: null, name: null },
    listeners: { available: true, count: 3 },
    next_track: playing && nextTrack
      ? { id: nextTrack.id, title: nextTrack.title, artist: nextTrack.artist, duration: nextTrack.duration }
      : null,
    next_schedule_change: { at: nowIso(2 * 3600 * 1000), rule_name: 'Ночной эфир' },
    stats_available: true,
    queue_revision: state.queueRevision,
    history_revision: state.historyRevision,
  };
}

function historyPayload() {
  return {
    items: [
      {
        id: '1',
        track_id: 't1',
        title: byId.get('t1').title,
        artist: byId.get('t1').artist,
        album: byId.get('t1').album,
        started_at: nowIso(-96000),
        ended_at: null,
        source_kind: 'library',
        source_ref: 'library',
      },
      {
        id: '2',
        track_id: 't2',
        title: byId.get('t2').title,
        artist: byId.get('t2').artist,
        album: byId.get('t2').album,
        started_at: nowIso(-300000),
        ended_at: nowIso(-96000),
        source_kind: 'library',
        source_ref: 'library',
      },
    ],
  };
}

function servicesPayload() {
  return {
    services: [
      {
        id: 'api',
        unit: 'vanilla-dream-radio-api.service',
        label: 'Backend',
        state: 'active',
        state_label: 'Работает',
        detail: 'Сервис отвечает',
        last_start: '2026-11-25T00:00:00+00:00',
        last_error: null,
      },
      {
        id: 'liquidsoap',
        unit: 'vanilla-dream-radio-liquidsoap.service',
        label: 'Liquidsoap',
        state: 'active',
        state_label: 'Работает',
        detail: 'systemctl is-active',
        last_start: '2026-11-25T00:00:00+00:00',
        last_error: null,
      },
      {
        id: 'icecast',
        unit: 'vanilla-dream-radio-icecast.service',
        label: 'Icecast',
        state: 'inactive',
        state_label: 'Остановлена',
        detail: 'systemctl is-active',
        last_start: null,
        last_error: null,
      },
    ],
    last_start: '2026-11-25T00:00:00+00:00',
    last_error: null,
  };
}

function storagePayload() {
  return {
    tracks: tracks.length,
    bytes: 74040000,
    cover_bytes: 120000,
    db_bytes: 65536,
    free_bytes: 41000000000,
    total_bytes: 97000000000,
    limit_gb: 0,
  };
}

function linksPayload() {
  return {
    links: [
      {
        key: 'mp3',
        label: 'Прямой MP3',
        url: `${BASE}/radio.mp3`,
        description: 'Прямая ссылка на аудиопоток для интернет-радиоплееров и совместимых приложений.',
        primary: true,
      },
      { key: 'player', label: 'Веб-плеер', url: `${BASE}/`, description: 'Публичная страница станции.' },
      { key: 'm3u', label: 'M3U-плейлист', url: `${BASE}/radio.m3u`, description: 'Для VLC и мобильных плееров.' },
      { key: 'pls', label: 'PLS-плейлист', url: `${BASE}/radio.pls`, description: 'Формат PLS.' },
      {
        key: 'metadata',
        label: 'Метаданные эфира',
        url: `${BASE}/api/public/now-playing`,
        description: 'JSON с текущим треком.',
      },
    ],
  };
}

function queuePayload() {
  return {
    current: {
      id: 'pos-current',
      position_id: 'pos-current',
      position: -1,
      track: byId.get(state.currentTrackId) ?? byId.get('t1'),
      source: effectiveSourcePayload(),
      started_at: state.startedAt,
      elapsed: 96,
    },
    items: state.queue.map((item, index) => ({
      id: item.id,
      position_id: item.id,
      position: index,
      track: byId.get(item.trackId),
      source: item.manual
        ? { kind: 'manual', label: 'Ручная вставка', name: 'Ручная вставка' }
        : effectiveSourcePayload(),
      prepared: Boolean(item.prepared),
      manual: Boolean(item.manual),
      estimated_start: nowIso(96000 + index * 200000),
    })),
    depth: 6,
    state_revision: state.revision,
  };
}

function queueResult(extra = {}) {
  return {
    ...queuePayload(),
    engine_synced: true,
    applied_revision: state.appliedRevision,
    ...extra,
  };
}

function json(res, code, body) {
  res.writeHead(code, {
    'Content-Type': 'application/json; charset=utf-8',
    'Cache-Control': 'no-store',
  });
  res.end(JSON.stringify(body));
}

function readBody(req) {
  return new Promise((resolve) => {
    let raw = '';
    req.on('data', (chunk) => {
      raw += chunk;
    });
    req.on('end', () => {
      try {
        resolve(JSON.parse(raw || '{}'));
      } catch {
        resolve({});
      }
    });
  });
}

function isAuth(req) {
  return String(req.headers.cookie || '').includes('mock_session=1');
}

const server = http.createServer(async (req, res) => {
  const url = new URL(req.url, BASE);
  const route = url.pathname;

  if (route === '/api/health') return json(res, 200, { ok: true });
  if (route === '/api/mock/reset') {
    resetMock();
    return json(res, 200, { ok: true });
  }
  if (route === '/api/mock/override') {
    if (url.searchParams.get('on') === '1') {
      const playlistId = url.searchParams.get('playlist') || 'p2';
      const playlist = state.playlists[playlistId];
      state.overrideInfo = {
        playlist_id: playlistId,
        name: playlist?.name ?? 'Временный плейлист',
        requested_at: nowIso(),
        until: nowIso(3600 * 1000),
      };
      state.revision += 1;
    } else {
      state.overrideInfo = null;
      state.revision += 1;
    }
    return json(res, 200, { override: overridePayload() });
  }
  if (route === '/api/mock/station') {
    const desired = url.searchParams.get('desired');
    if (desired === 'playing' || desired === 'stopped') {
      state.desired = desired;
      state.reason = desired === 'playing' ? 'playing' : 'stopped_by_owner';
      state.revision += 1;
    }
    return json(res, 200, { state: stationStatePayload() });
  }
  if (route === '/api/mock/program') {
    const playlistId = url.searchParams.get('playlist') || 'p1';
    const mode = url.searchParams.get('mode') === 'auto' ? 'auto' : 'playlist';
    state.mode = mode;
    state.selectedPlaylistId = playlistId;
    state.effectivePlaylistId = playlistId;
    state.programPlaylistId = playlistId;
    state.pendingSource = null;
    state.revision += 1;
    return json(res, 200, { state: stationStatePayload() });
  }
  if (route === '/api/mock/rotation-repeat') {
    state.rotationRepeat = url.searchParams.get('on') === '1';
    state.revision += 1;
    return json(res, 200, { rotation: state.rotationRepeat });
  }
  if (route === '/api/mock/delay') {
    state.delayMs = Math.max(0, Number(url.searchParams.get('ms') || 0));
    return json(res, 200, { delay: state.delayMs });
  }
  if (route === '/api/mock/audio-delay') {
    state.audioDelayMs = Math.max(0, Number(url.searchParams.get('ms') || 0));
    return json(res, 200, { delay: state.audioDelayMs });
  }
  if (route === '/api/mock/sync-pending') {
    state.pendingSyncOnAdd = url.searchParams.get('on') === '1';
    return json(res, 200, { pending: state.pendingSyncOnAdd });
  }
  if (route === '/api/mock/sync-complete') {
    state.pendingSyncOnAdd = false;
    state.appliedRevision = state.revision;
    state.queueRevision += 1;
    return json(res, 200, { state: stationStatePayload() });
  }
  if (route === '/api/mock/advance') {
    const next = state.queue.shift();
    if (next) {
      state.currentTrackId = next.trackId;
      state.startedAt = nowIso();
      if (state.pendingSource) {
        state.mode = state.pendingSource.mode;
        state.selectedPlaylistId = state.pendingSource.playlist_id;
        state.effectivePlaylistId = state.pendingSource.playlist_id;
        state.programPlaylistId = state.pendingSource.playlist_id;
        state.pendingSource = null;
      }
      state.currentSource = next.manual ? manualSourcePayload() : effectiveSourcePayload();
      state.queueRevision += 1;
      state.historyRevision += 1;
      state.revision += 1;
      state.appliedRevision = state.revision;
    }
    return json(res, 200, { ok: true, state: stationStatePayload() });
  }
  if (route === '/api/mock/queue') {
    state.queue = (url.searchParams.get('ids') || 't2,t3,t4,t5')
      .split(',')
      .map((trackId, index) => ({
        id: `pos-${index + 2}`,
        trackId,
        manual: false,
        prepared: index < 2,
      }));
    state.queueRevision += 1;
    return json(res, 200, { queue: queuePayload() });
  }
  if (route === '/mock-cover.svg') {
    res.writeHead(200, { 'Content-Type': 'image/svg+xml' });
    return res.end(coverSvg);
  }

  if (route === '/api/auth/me') {
    return json(res, 200, isAuth(req) ? { authenticated: true, csrf_token: 'csrf-test' } : { authenticated: false });
  }
  if (route === '/api/auth/login' && req.method === 'POST') {
    res.setHeader('Set-Cookie', 'mock_session=1; Path=/; HttpOnly; SameSite=Lax');
    return json(res, 200, { ok: true, csrf_token: 'csrf-test' });
  }
  if (route === '/api/auth/logout') {
    res.setHeader('Set-Cookie', 'mock_session=; Path=/; Max-Age=0');
    return json(res, 200, { ok: true });
  }
  if (route === '/api/auth/password') return json(res, 200, { ok: true });

  if (route === '/api/public/now-playing') return json(res, 200, nowPlaying(req));
  if (route === '/api/public/events') {
    res.writeHead(200, {
      'Content-Type': 'text/event-stream; charset=utf-8',
      'Cache-Control': 'no-cache',
      Connection: 'keep-alive',
    });
    const send = () => {
      try {
        res.write(`data: ${JSON.stringify(nowPlaying(req))}\n\n`);
      } catch {
        /* closed */
      }
    };
    send();
    const timer = setInterval(send, 1000);
    req.on('close', () => clearInterval(timer));
    return;
  }
  if (route === '/api/public/history') return json(res, 200, { items: historyPayload().items.slice(0, 6) });

  if (route.startsWith('/api/admin/')) {
    if (!isAuth(req)) return json(res, 401, { detail: { code: 'auth_required', message: 'Требуется вход' } });

    if (route === '/api/admin/tracks' && req.method === 'GET') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const limit = Number(url.searchParams.get('limit') || 50);
      const offset = Number(url.searchParams.get('offset') || 0);
      const filtered = tracks.filter(
        (track) =>
          !track.pending_delete &&
          (!q || `${track.title} ${track.artist} ${track.album}`.toLowerCase().includes(q)),
      );
      return json(res, 200, {
        items: filtered.slice(offset, offset + limit).map(trackWithCounts),
        total: filtered.length,
        library: { count: tracks.length, bytes: 74040000 },
      });
    }
    if (route === '/api/admin/tracks/ids') {
      const q = (url.searchParams.get('q') || '').toLowerCase();
      const exclude = url.searchParams.get('exclude_playlist_id');
      const existing = exclude && exclude !== 'library' ? state.playlists[exclude]?.membership ?? new Set() : new Set();
      const ids = tracks
        .filter((track) => !q || `${track.title} ${track.artist} ${track.album}`.toLowerCase().includes(q))
        .filter((track) => !existing.has(track.id))
        .map((track) => track.id);
      return json(res, 200, { ids, total: ids.length });
    }

    if (route === '/api/admin/tracks/upload' && req.method === 'POST') {
      const buffer = await readRaw(req);
      const filename = multipartFilename(buffer);
      const requestedBatch = multipartField(buffer, 'batch_id');
      let batch = requestedBatch ? stateMedia.batches.get(requestedBatch) : null;
      if (!batch) {
        batch = { id: requestedBatch || nextId('b'), items: [], cancelled: false };
        stateMedia.batches.set(batch.id, batch);
      }
      const lower = filename.toLowerCase();
      const base = filename.replace(/\.[^.]+$/, '');
      const size = Math.max(1024, buffer.length);
      if (lower.includes('bad')) {
        const item = {
          id: nextId('u'),
          filename,
          size,
          state: 'error',
          reason: null,
          metadata: null,
          matches: [],
          decision: null,
          error: 'Файл повреждён или не читается',
        };
        batch.items.push(item);
        return json(res, 422, {
          detail: { code: 'invalid_audio', message: 'Файл повреждён или не читается' },
        });
      }
      if (lower.includes('dup') || lower.includes('copy') || lower.includes('same')) {
        const kind = lower.includes('same') ? 'content' : lower.includes('possible') ? 'possible' : 'tags';
        const match = byId.get('t1');
        const metadata = {
          title: base,
          artist: 'Новый артист',
          album: 'Копии',
          duration: 201,
          size,
          bitrate: 320,
          cover_url: COVER,
        };
        const item = {
          id: nextId('u'),
          filename,
          size,
          state: 'pending',
          reason: kind,
          metadata,
          matches: [
            { duplicate_kind: kind, track: comparison(match) },
            ...(kind === 'tags'
              ? [{ duplicate_kind: 'possible', track: comparison(byId.get('t3')) }]
              : []),
          ],
          decision: null,
          error: null,
        };
        batch.items.push(item);
        return json(res, 202, {
          status: 'pending',
          pending: {
            id: item.id,
            reason: kind,
            metadata,
            matches: item.matches,
            decision: null,
          },
          batch_id: batch.id,
        });
      }
      const id = nextId('up');
      const track = {
        id,
        title: base,
        artist: 'Новый артист',
        album: 'Свежие загрузки',
        genre: 'Ambient',
        year: 2026,
        duration: 187,
        bitrate: 320,
        size,
        ext: 'mp3',
        cover_url: COVER,
        cover_kind: 'file',
        is_duplicate: false,
        duplicate_kind: null,
        duplicate_of: null,
        can_revert_cover: false,
        source_filename: filename,
        play_count: 0,
        pending_delete: false,
        created_at: nowIso(),
        updated_at: nowIso(),
        stream_url: `/api/admin/tracks/${id}/stream`,
      };
      tracks.unshift(track);
      byId.set(id, track);
      state.revision += 1;
      if (state.programPlaylistId === 'library' && state.desired === 'playing') {
        state.appliedRevision = state.pendingSyncOnAdd ? state.revision - 1 : state.revision;
        state.queueRevision += 1;
        state.queue.push({ id: nextId('pos'), trackId: id, manual: false, prepared: false });
      }
      batch.items.push({
        id: nextId('u'),
        filename,
        size,
        state: 'added',
        reason: null,
        metadata: null,
        matches: [],
        decision: null,
        error: null,
      });
      const activeLibrary =
        state.programPlaylistId === 'library' && state.desired === 'playing';
      const application = activeLibrary
        ? state.pendingSyncOnAdd
          ? 'pending'
          : 'applied'
        : 'saved';
      return json(res, 201, {
        status: 'added',
        track,
        batch_id: batch.id,
        application,
        in_current_cycle: activeLibrary,
        engine_synced: application === 'applied',
        state_revision: state.revision,
        applied_revision: state.appliedRevision,
      });
    }

    const batchMatch = route.match(/^\/api\/admin\/uploads\/([^/]+)$/);
    if (batchMatch && req.method === 'GET') {
      const batch = stateMedia.batches.get(decodeURIComponent(batchMatch[1]));
      if (!batch) return json(res, 404, { detail: { code: 'batch_not_found', message: 'Пакет не найден' } });
      return json(res, 200, { batch: batchPayload(batch) });
    }

    function applyDecision(item, action) {
      item.decision = action;
      item.state = action === 'save' ? 'saved' : 'skipped';
      if (action === 'save') {
        const id = nextId('up');
        const source = item.matches[0]?.track;
        const track = {
          id,
          title: item.metadata?.title || item.filename,
          artist: item.metadata?.artist || 'Новый артист',
          album: item.metadata?.album || 'Копии',
          genre: 'Ambient',
          year: 2026,
          duration: item.metadata?.duration ?? 201,
          bitrate: item.metadata?.bitrate ?? 320,
          size: item.metadata?.size ?? item.size,
          ext: 'mp3',
          cover_url: item.metadata?.cover_url ?? COVER,
          cover_kind: 'file',
          is_duplicate: true,
          duplicate_kind: item.reason,
          duplicate_of: source?.id ?? 't1',
          can_revert_cover: false,
          source_filename: item.filename,
          play_count: 0,
          pending_delete: false,
          created_at: nowIso(),
          updated_at: nowIso(),
          stream_url: `/api/admin/tracks/${id}/stream`,
        };
        tracks.unshift(track);
        byId.set(id, track);
        if (state.programPlaylistId === 'library') {
          state.revision += 1;
          state.appliedRevision = state.revision;
          state.queueRevision += 1;
          state.queue.push({ id: nextId('pos'), trackId: id, manual: false, prepared: false });
        }
      }
    }

    const decideMatch = route.match(/^\/api\/admin\/uploads\/([^/]+)\/decide$/);
    if (decideMatch && req.method === 'POST') {
      const body = await readBody(req);
      const action = body.action === 'skip' ? 'skip' : 'save';
      let found = null;
      for (const batch of stateMedia.batches.values()) {
        const item = batch.items.find((entry) => entry.id === decodeURIComponent(decideMatch[1]));
        if (item) found = { batch, item };
      }
      if (!found) return json(res, 404, { detail: { code: 'pending_not_found', message: 'Файл не найден' } });
      if (found.item.decision) {
        if (found.item.decision === action) return json(res, 200, { ok: true, already: true });
        return json(res, 409, {
          detail: { code: 'already_decided', message: 'Решение уже принято' },
        });
      }
      applyDecision(found.item, action);
      return json(res, 200, { ok: true, batch: batchPayload(found.batch) });
    }

    const batchDecideMatch = route.match(/^\/api\/admin\/uploads\/batch\/([^/]+)\/decide$/);
    if (batchDecideMatch && req.method === 'POST') {
      const body = await readBody(req);
      const action = body.action === 'skip' ? 'skip' : 'save';
      const query = String(body.query || '').toLowerCase();
      const batch = stateMedia.batches.get(decodeURIComponent(batchDecideMatch[1]));
      if (!batch) return json(res, 404, { detail: { code: 'batch_not_found', message: 'Пакет не найден' } });
      for (const item of batch.items) {
        if (item.state !== 'pending' || item.decision) continue;
        if (query && !`${item.filename} ${item.metadata?.title || ''} ${item.metadata?.artist || ''}`.toLowerCase().includes(query)) {
          continue;
        }
        applyDecision(item, action);
      }
      return json(res, 200, { ok: true, batch: batchPayload(batch) });
    }

    const batchCancelMatch = route.match(/^\/api\/admin\/uploads\/batch\/([^/]+)\/cancel$/);
    if (batchCancelMatch && req.method === 'POST') {
      const batch = stateMedia.batches.get(decodeURIComponent(batchCancelMatch[1]));
      if (!batch) return json(res, 404, { detail: { code: 'batch_not_found', message: 'Пакет не найден' } });
      for (const item of batch.items) {
        if (item.state === 'pending' && !item.decision) applyDecision(item, 'skip');
      }
      batch.cancelled = true;
      return json(res, 200, { ok: true, batch: batchPayload(batch) });
    }

    const downloadMatch = route.match(/^\/api\/admin\/tracks\/([^/]+)\/download$/);
    if (downloadMatch && req.method === 'GET') {
      const track = byId.get(decodeURIComponent(downloadMatch[1]));
      if (!track) return json(res, 404, { detail: { code: 'track_not_found', message: 'Трек не найден' } });
      const mode = url.searchParams.get('mode') === 'original' ? 'original' : 'tags';
      const filename = `${track.artist || 'Без исполнителя'} - ${track.title}.${track.ext || 'mp3'}`;
      res.writeHead(200, {
        'Content-Type': 'audio/mpeg',
        'Content-Disposition': `attachment; filename*=UTF-8''${encodeURIComponent(filename)}`,
        'X-Mock-Mode': mode,
      });
      return res.end(Buffer.from(`MOCK-AUDIO:${track.id}:${mode}`));
    }

    const trackCoverMatch = route.match(/^\/api\/admin\/tracks\/([^/]+)\/cover$/);
    if (trackCoverMatch && (req.method === 'POST' || req.method === 'DELETE')) {
      const track = byId.get(decodeURIComponent(trackCoverMatch[1]));
      if (!track) return json(res, 404, { detail: { code: 'track_not_found', message: 'Трек не найден' } });
      if (req.method === 'POST') {
        await readRaw(req);
        track.cover_url = `${COVER}?v=${Date.now()}`;
        track.cover_kind = 'custom';
        track.can_revert_cover = true;
      } else {
        track.cover_url = COVER;
        track.cover_kind = 'file';
        track.can_revert_cover = false;
      }
      track.updated_at = nowIso();
      return json(res, 200, { track });
    }

    const playNowMatch = route.match(/^\/api\/admin\/tracks\/([^/]+)\/play-now$/);
    if (playNowMatch && req.method === 'POST') {
      const id = decodeURIComponent(playNowMatch[1]);
      if (byId.has(id)) {
        if (state.currentTrackId) {
          state.queue.unshift({
            id: nextId('pos'),
            trackId: state.currentTrackId,
            manual: false,
            prepared: false,
          });
        }
        state.currentTrackId = id;
        state.currentSource = manualSourcePayload();
        state.startedAt = nowIso();
        state.queue = state.queue.filter((item) => item.trackId !== id);
        state.queueRevision += 1;
        state.historyRevision += 1;
        state.revision += 1;
        state.appliedRevision = state.revision;
      }
      return json(res, 200, queueResult());
    }
    const playNextMatch = route.match(/^\/api\/admin\/tracks\/([^/]+)\/play-next$/);
    if (playNextMatch && req.method === 'POST') {
      const id = decodeURIComponent(playNextMatch[1]);
      if (byId.has(id)) {
        state.queue = state.queue.filter((item) => item.trackId !== id);
        state.queue.unshift({ id: nextId('pos'), trackId: id, manual: true, prepared: false });
        state.queueRevision += 1;
        state.revision += 1;
        state.appliedRevision = state.revision;
      }
      return json(res, 200, queueResult());
    }
    const enqueueMatch = route.match(/^\/api\/admin\/tracks\/([^/]+)\/queue$/);
    if (enqueueMatch && req.method === 'POST') {
      const id = decodeURIComponent(enqueueMatch[1]);
      if (byId.has(id) && !state.queue.some((item) => item.trackId === id)) {
        state.queue.push({ id: nextId('pos'), trackId: id, manual: true, prepared: false });
        state.queueRevision += 1;
        state.revision += 1;
        state.appliedRevision = state.revision;
      }
      return json(res, 200, queueResult());
    }
    if (route === '/api/admin/tracks/bulk-delete' && req.method === 'POST') {
      const body = await readBody(req);
      const mode = body.mode === 'now' ? 'now' : 'after';
      const marked = [];
      const notFound = [];
      const currentIds = (body.ids || []).includes(state.currentTrackId)
        ? [state.currentTrackId]
        : [];
      const current = currentIds.length > 0;
      for (const id of body.ids || []) {
        const track = byId.get(id);
        if (!track) {
          notFound.push(id);
          continue;
        }
        track.pending_delete = true;
        marked.push(id);
        state.queue = state.queue.filter((item) => item.trackId !== id);
      }
      let skipped = false;
      if (mode === 'now' && current) {
        const next = state.queue.shift();
        if (next) {
          state.currentTrackId = next.trackId;
          state.currentSource = next.manual ? manualSourcePayload() : effectiveSourcePayload();
        }
        skipped = true;
      }
      state.queueRevision += 1;
      state.revision += 1;
      state.appliedRevision = state.revision;
      return json(res, 200, {
        marked,
        deleted: marked,
        purged: [],
        pending: marked,
        not_found: notFound,
        current: currentIds,
        mode,
        skipped,
      });
    }
    if (/^\/api\/admin\/tracks\/[^/]+$/.test(route) && (req.method === 'PUT' || req.method === 'PATCH')) {
      let patch;
      const track = byId.get(route.split('/').pop());
      if (req.method === 'PUT') {
        const buffer = await readRaw(req);
        patch = {
          title: multipartField(buffer, 'title') || track?.title,
          artist: multipartField(buffer, 'artist') || null,
          album: multipartField(buffer, 'album') || null,
          cover_action: multipartField(buffer, 'cover_action') || 'keep',
        };
        if (patch.cover_action === 'replace') {
          track.cover_kind = 'custom';
          track.cover_url = `${COVER}?v=${Date.now()}`;
          track.can_revert_cover = true;
        } else if (patch.cover_action === 'revert') {
          track.cover_kind = 'file';
          track.cover_url = track.original_cover_url ?? COVER;
          track.can_revert_cover = false;
        }
        delete patch.cover_action;
      } else {
        patch = await readBody(req);
      }
      if (track) {
        Object.assign(track, patch);
        track.updated_at = nowIso();
      }
      return json(res, 200, { track });
    }
    if (/^\/api\/admin\/tracks\/[^/]+$/.test(route) && req.method === 'DELETE') {
      const body = await readBody(req);
      const mode = body.mode === 'now' ? 'now' : 'after';
      const id = route.split('/').pop();
      const track = byId.get(id);
      if (!track) return json(res, 404, { detail: { code: 'track_not_found', message: 'Трек не найден' } });
      track.pending_delete = true;
      for (const playlist of Object.values(state.playlists)) {
        playlist.membership.delete(id);
      }
      state.queue = state.queue.filter((item) => item.trackId !== id);
      if (mode === 'now' && state.currentTrackId === id) {
        const next = state.queue.shift();
        if (next) {
          state.currentTrackId = next.trackId;
          state.currentSource = next.manual ? manualSourcePayload() : effectiveSourcePayload();
          state.startedAt = nowIso();
        }
      }
      state.queueRevision += 1;
      state.revision += 1;
      state.appliedRevision = state.revision;
      return json(res, 200, {
        deleted: true,
        pending: mode === 'after',
        playlists_count: 1,
        mode,
        current: state.currentTrackId === id,
        skipped: mode === 'now' && state.currentTrackId !== id,
      });
    }
    if (/^\/api\/admin\/tracks\/[^/]+\/stream$/.test(route)) {
      res.writeHead(204);
      return res.end();
    }

    if (route === '/api/admin/playlists' && req.method === 'GET') {
      const items = [playlistPayload('library'), ...Object.keys(state.playlists).map(playlistPayload)];
      return json(res, 200, { items });
    }
    if (route === '/api/admin/playlists' && req.method === 'POST') {
      const body = await readBody(req);
      const id = `p${Object.keys(state.playlists).length + 1}`;
      state.playlists[id] = {
        id,
        name: body.name || 'Новый плейлист',
        description: body.description || '',
        shuffle: false,
        repeat_mode: 'none',
        cover_kind: 'none',
        cover_url: null,
        membership: new Set(),
        updated_at: nowIso(),
      };
      return json(res, 201, { playlist: playlistPayload(id) });
    }
    const playlistMatch = route.match(/^\/api\/admin\/playlists\/([^/]+)$/);
    if (playlistMatch && req.method === 'GET') {
      return json(res, 200, detailPayload(decodeURIComponent(playlistMatch[1])));
    }
    if (playlistMatch && req.method === 'PATCH') {
      const id = decodeURIComponent(playlistMatch[1]);
      const body = await readBody(req);
      if (state.delayMs > 0) await new Promise((resolve) => setTimeout(resolve, state.delayMs));
      const providedRevision = body.state_revision === undefined ? null : Number(body.state_revision);
      if (providedRevision !== null && providedRevision < state.revision) {
        return json(res, 409, {
          detail: { code: 'stale_state', message: 'Состояние изменилось. Обновите страницу.' },
        });
      }
      const playlist = state.playlists[id];
      let changed = false;
      if (id === 'library') {
        if (typeof body.shuffle === 'boolean') {
          changed = changed || body.shuffle !== state.systemSettings.shuffle;
          state.systemSettings.shuffle = body.shuffle;
        }
        if (typeof body.repeat_mode === 'string') {
          changed = changed || body.repeat_mode !== state.systemSettings.repeat_mode;
          state.systemSettings.repeat_mode = body.repeat_mode;
        }
      } else if (playlist) {
        if (typeof body.name === 'string') playlist.name = body.name;
        if ('description' in body) playlist.description = body.description ?? '';
        if (typeof body.shuffle === 'boolean') {
          changed = changed || body.shuffle !== playlist.shuffle;
          playlist.shuffle = body.shuffle;
        }
        if (typeof body.repeat_mode === 'string') {
          changed = changed || body.repeat_mode !== playlist.repeat_mode;
          playlist.repeat_mode = body.repeat_mode;
        }
        playlist.updated_at = nowIso();
      }
      const isActive = state.programPlaylistId === id && state.desired === 'playing';
      let application = 'saved';
      if (changed) {
        state.revision += 1;
        if (isActive) {
          application = state.pendingSyncOnAdd ? 'pending' : 'applied';
          state.appliedRevision = application === 'applied' ? state.revision : state.appliedRevision;
          state.queueRevision += 1;
        }
      }
      return json(res, 200, {
        playlist: playlistPayload(id),
        application,
        engine_synced: application === 'applied',
        state_revision: state.revision,
        applied_revision: state.appliedRevision,
      });
    }
    if (playlistMatch && req.method === 'DELETE') {
      delete state.playlists[decodeURIComponent(playlistMatch[1])];
      return json(res, 200, { deleted: true });
    }

    const tracksMatch = route.match(/^\/api\/admin\/playlists\/([^/]+)\/tracks$/);
    if (tracksMatch && req.method === 'POST') {
      const id = decodeURIComponent(tracksMatch[1]);
      if (id === 'library') {
        return json(res, 409, { detail: { code: 'system_playlist', message: 'Системный плейлист изменяется автоматически' } });
      }
      const body = await readBody(req);
      const playlist = state.playlists[id];
      let added = 0;
      let skipped = 0;
      let unavailable = 0;
      for (const trackId of new Set(body.track_ids || [])) {
        const track = byId.get(trackId);
        if (!track || track.pending_delete) unavailable += 1;
        else if (playlist.membership.has(trackId)) skipped += 1;
        else {
          playlist.membership.add(trackId);
          added += 1;
        }
      }
      playlist.updated_at = nowIso();
      const isActive = state.programPlaylistId === id && state.desired === 'playing';
      let application = 'saved';
      if (added > 0) {
        state.revision += 1;
        application = isActive
          ? state.pendingSyncOnAdd
            ? 'pending'
            : 'applied'
          : 'saved';
        if (isActive) {
          state.appliedRevision =
            application === 'applied' ? state.revision : state.appliedRevision;
          state.queueRevision += 1;
          for (const trackId of new Set(body.track_ids || [])) {
            if (byId.has(trackId) && !state.queue.some((item) => item.trackId === trackId)) {
              state.queue.push({
                id: nextId('pos'),
                trackId,
                manual: false,
                prepared: false,
              });
            }
          }
        }
      }
      return json(res, 200, {
        added,
        skipped,
        unavailable,
        application,
        in_current_cycle: isActive && added > 0,
        engine_synced: application === 'applied',
        state_revision: state.revision,
        applied_revision: state.appliedRevision,
        queue_changed: added > 0 && isActive,
        playlist: playlistPayload(id),
      });
    }
    const removeMatch = route.match(/^\/api\/admin\/playlists\/([^/]+)\/tracks\/([^/]+)$/);
    if (removeMatch && req.method === 'DELETE') {
      const id = decodeURIComponent(removeMatch[1]);
      state.playlists[id]?.membership.delete(decodeURIComponent(removeMatch[2]));
      return json(res, 200, { removed: true, playlist: playlistPayload(id) });
    }
    const orderMatch = route.match(/^\/api\/admin\/playlists\/([^/]+)\/order$/);
    if (orderMatch && req.method === 'PUT') {
      const id = decodeURIComponent(orderMatch[1]);
      const body = await readBody(req);
      const playlist = state.playlists[id];
      if (playlist) playlist.membership = new Set(body.track_ids || []);
      return json(res, 200, { playlist: playlistPayload(id) });
    }
    const playMatch = route.match(/^\/api\/admin\/playlists\/([^/]+)\/play$/);
    if (playMatch && req.method === 'POST') {
      const playlistId = decodeURIComponent(playMatch[1]);
      const body = await readBody(req);
      const mode = body.mode === 'next' ? 'next' : 'now';
      const restart = Boolean(body.restart);
      if (
        state.mode === 'playlist' &&
        state.effectivePlaylistId === playlistId &&
        state.selectedPlaylistId === playlistId &&
        !restart &&
        !state.pendingSource &&
        mode === 'now' &&
        state.desired === 'playing'
      ) {
        return json(res, 200, {
          status: 'already_active',
          started: false,
          engine_synced: true,
          station: stationStatePayload(),
          playlist: playlistPayload(playlistId),
          state_revision: state.revision,
          applied_revision: state.appliedRevision,
        });
      }
      const wasStopped = state.desired === 'stopped';
      state.desired = 'playing';
      state.mode = 'playlist';
      state.selectedPlaylistId = playlistId;
      state.overrideInfo = null;
      state.revision += 1;
      if (mode === 'next') {
        state.pendingSource = {
          playlist_id: playlistId,
          mode: 'playlist',
          requested_at: nowIso(),
        };
      } else {
        state.pendingSource = null;
        state.effectivePlaylistId = playlistId;
        state.programPlaylistId = playlistId;
        state.status = 'starting';
        state.reason = 'engine_starting';
        state.appliedRevision = state.revision;
        state.queueRevision += 1;
        setTimeout(() => {
          state.status = 'on_air';
          state.reason = 'playing';
          const first = playlistTracks(playlistId)[0];
          if (first) state.currentTrackId = first.id;
          state.currentSource = effectiveSourcePayload();
          state.startedAt = nowIso();
          state.revision += 1;
          state.historyRevision += 1;
        }, 700);
      }
      return json(res, 200, {
        started: wasStopped,
        status: 'switching',
        engine_synced: true,
        station: stationStatePayload(),
        playlist: playlistPayload(playlistId),
        state_revision: state.revision,
        applied_revision: state.appliedRevision,
      });
    }

    const restartMatch = route.match(/^\/api\/admin\/playlists\/([^/]+)\/restart$/);
    if (restartMatch && req.method === 'POST') {
      const playlistId = decodeURIComponent(restartMatch[1]);
      state.mode = 'playlist';
      state.selectedPlaylistId = playlistId;
      state.effectivePlaylistId = playlistId;
      state.programPlaylistId = playlistId;
      state.pendingSource = null;
      state.overrideInfo = null;
      state.revision += 1;
      state.appliedRevision = state.revision;
      state.queueRevision += 1;
      state.startedAt = nowIso();
      return json(res, 200, {
        status: 'switching',
        started: false,
        engine_synced: true,
        station: stationStatePayload(),
        playlist: playlistPayload(playlistId),
        state_revision: state.revision,
        applied_revision: state.appliedRevision,
      });
    }
    const coverMatch = route.match(/^\/api\/admin\/playlists\/([^/]+)\/cover$/);
    if (coverMatch) {
      const id = decodeURIComponent(coverMatch[1]);
      const playlist = state.playlists[id];
      if (req.method === 'POST') {
        if (playlist) {
          playlist.cover_kind = 'custom';
          playlist.cover_url = `${COVER}?v=${Date.now()}`;
          playlist.updated_at = nowIso();
        }
        return json(res, 200, { playlist: playlistPayload(id) });
      }
      if (req.method === 'DELETE') {
        if (playlist) {
          playlist.cover_kind = 'auto';
          playlist.cover_url = `${COVER}?v=${Date.now()}`;
          playlist.updated_at = nowIso();
        }
        return json(res, 200, { playlist: playlistPayload(id) });
      }
    }
    const rotationMatch = route.match(/^\/api\/admin\/playlists\/([^/]+)\/rotation$/);
    if (rotationMatch && req.method === 'POST') return json(res, 200, { ok: true });

    if (route === '/api/admin/queue') return json(res, 200, queueResult());
    if (route === '/api/admin/queue/shuffle' && req.method === 'POST') {
      const manual = state.queue.filter((item) => item.manual);
      const automatic = state.queue.filter((item) => !item.manual);
      for (let index = automatic.length - 1; index > 0; index -= 1) {
        const swap = Math.floor(Math.random() * (index + 1));
        [automatic[index], automatic[swap]] = [automatic[swap], automatic[index]];
      }
      state.queue = [...automatic, ...manual];
      state.queueRevision += 1;
      state.revision += 1;
      state.appliedRevision = state.revision;
      return json(res, 200, queueResult({ shuffled: automatic.length }));
    }
    if (route === '/api/admin/queue/clear-manual' && req.method === 'POST') {
      const removed = state.queue.filter((item) => item.manual).length;
      state.queue = state.queue.filter((item) => !item.manual);
      state.queueRevision += 1;
      state.revision += 1;
      state.appliedRevision = state.revision;
      return json(res, 200, queueResult({ removed }));
    }
    const queueMoveMatch = route.match(/^\/api\/admin\/queue\/([^/]+)$/);
    if (queueMoveMatch && req.method === 'PATCH') {
      const body = await readBody(req);
      const id = decodeURIComponent(queueMoveMatch[1]);
      const from = state.queue.findIndex((item) => item.id === id);
      const target = Number(body.move_to);
      if (from >= 0 && Number.isInteger(target) && target >= 0) {
        const [item] = state.queue.splice(from, 1);
        state.queue.splice(Math.min(target, state.queue.length), 0, item);
        state.queueRevision += 1;
        state.revision += 1;
        state.appliedRevision = state.revision;
      }
      return json(res, 200, queueResult());
    }
    if (queueMoveMatch && req.method === 'DELETE') {
      const id = decodeURIComponent(queueMoveMatch[1]);
      state.queue = state.queue.filter((item) => item.id !== id);
      state.queueRevision += 1;
      state.revision += 1;
      state.appliedRevision = state.revision;
      return json(res, 200, queueResult());
    }
    const queueNextMatch = route.match(/^\/api\/admin\/queue\/([^/]+)\/move-next$/);
    if (queueNextMatch && req.method === 'POST') {
      const id = decodeURIComponent(queueNextMatch[1]);
      const from = state.queue.findIndex((item) => item.id === id);
      if (from > 0) {
        const [item] = state.queue.splice(from, 1);
        state.queue.unshift(item);
        state.queueRevision += 1;
        state.revision += 1;
        state.appliedRevision = state.revision;
      }
      return json(res, 200, queueResult());
    }
    if (route === '/api/admin/history') return json(res, 200, historyPayload());
    if (route === '/api/admin/settings' && req.method === 'GET') {
      return json(res, 200, { ...state.settings, settings: state.settings, supported: { bitrates: [96, 112, 128, 160, 192, 224, 256, 320] } });
    }
    if (route === '/api/admin/settings' && req.method === 'PUT') {
      const body = await readBody(req);
      const patch = body.settings || body;
      Object.assign(state.settings, patch);
      state.desired = state.settings.desired_state || state.desired;
      return json(res, 200, { ...state.settings, settings: state.settings, engine_restarted: false });
    }
    if (route === '/api/admin/storage') return json(res, 200, storagePayload());
    if (route === '/api/admin/services') return json(res, 200, servicesPayload());
    if (route === '/api/admin/links') return json(res, 200, linksPayload());
    if (route === '/api/admin/station/status') return json(res, 200, nowPlaying(req));
    if (route === '/api/admin/station/state' && req.method === 'GET') {
      return json(res, 200, stationStatePayload());
    }
    if (route === '/api/admin/station/mode' && req.method === 'PUT') {
      const body = await readBody(req);
      const mode = body.mode === 'auto' ? 'auto' : 'playlist';
      state.mode = mode;
      if (mode === 'playlist') {
        const playlistId = body.playlist_id || state.selectedPlaylistId || 'library';
        state.selectedPlaylistId = playlistId;
        state.effectivePlaylistId = playlistId;
        state.programPlaylistId = playlistId;
      }
      state.pendingSource = null;
      state.revision += 1;
      state.appliedRevision = state.revision;
      state.queueRevision += 1;
      const payload = stationStatePayload();
      return json(res, 200, {
        ...payload,
        station: payload,
        engine_synced: true,
        applied_revision: state.appliedRevision,
      });
    }
    if (route === '/api/admin/station/override' && req.method === 'POST') {
      const body = await readBody(req);
      const playlistId = body.playlist_id || 'p2';
      state.overrideInfo = {
        playlist_id: playlistId,
        name: playlistRecord(playlistId)?.name ?? 'Временный плейлист',
        requested_at: nowIso(),
        until: body.scope === 'indefinite' ? null : nowIso(3600 * 1000),
      };
      state.revision += 1;
      return json(res, 200, stationStatePayload());
    }
    if (route === '/api/admin/station/override' && req.method === 'DELETE') {
      state.overrideInfo = null;
      state.revision += 1;
      return json(res, 200, stationStatePayload());
    }
    const stationAction = route.match(/^\/api\/admin\/station\/(start|stop|restart|skip)$/);
    if (stationAction) {
      const action = stationAction[1];
      if (action === 'start') {
        state.desired = 'playing';
        state.status = 'starting';
        state.reason = 'engine_starting';
        setTimeout(() => {
          state.status = 'on_air';
          state.reason = 'playing';
          state.revision += 1;
        }, 500);
      } else if (action === 'stop') {
        state.desired = 'stopped';
        state.status = 'off';
        state.reason = 'stopped_by_owner';
      } else if (action === 'restart') {
        state.status = 'starting';
        state.reason = 'engine_starting';
      } else if (action === 'skip') {
        const next = state.queue.shift();
        if (next) {
          state.currentTrackId = next.trackId;
          state.currentSource = next.manual ? manualSourcePayload() : effectiveSourcePayload();
          state.startedAt = nowIso();
          state.queueRevision += 1;
          state.historyRevision += 1;
        }
      }
      state.revision += 1;
      return json(res, 200, { ok: true });
    }
    if (route === '/api/admin/schedule') {
      return json(res, 200, { timezone: 'Asia/Magadan', schedule_enabled: true, rules: [] });
    }
    if (route === '/api/admin/schedule/conflicts') return json(res, 200, { conflicts: [] });
    if (route === '/api/admin/rotation') return json(res, 200, { mode: 'sequential', items: [] });
    if (route === '/api/admin/rotation/next-preview') return json(res, 200, { playlist_id: 'library', playlist_name: 'Вся медиатека' });

    if (route === '/api/admin/exports' && req.method === 'POST') {
      const body = await readBody(req);
      const metadata = body.metadata === 'original' ? 'original' : 'tags';
      const scope = body.scope === 'playlist' || body.scope === 'tracks' ? body.scope : 'library';
      let composition;
      if (scope === 'tracks') {
        composition = (Array.isArray(body.ids) ? body.ids : []).map((id) => byId.get(id)).filter(Boolean);
      } else if (scope === 'playlist') {
        composition = playlistTracks(body.playlist_id);
      } else {
        composition = tracks;
      }
      const names = composition.map((track) => ({
        track,
        name: `${track.artist || 'Без исполнителя'} - ${track.title}.${track.ext || 'mp3'}`,
      }));
      if (body.format === 'files') {
        return json(res, 200, {
          files: names.map(({ track, name }) => ({
            track_id: track.id,
            name,
            size: track.size,
            url: `/api/admin/tracks/${encodeURIComponent(track.id)}/download?mode=${metadata}`,
          })),
        });
      }
      const job = {
        id: nextId('e'),
        state: 'preparing',
        processed: 0,
        total: names.length,
        archive: null,
        error: null,
        polls: 0,
        expires_at: nowIso(6 * 3600 * 1000),
      };
      stateMedia.exports.set(job.id, job);
      return json(res, 202, { job: exportJobPayload(job) });
    }

    const exportMatch = route.match(/^\/api\/admin\/exports\/([^/]+)$/);
    if (exportMatch && req.method === 'GET') {
      const job = stateMedia.exports.get(decodeURIComponent(exportMatch[1]));
      if (!job) return json(res, 404, { detail: { code: 'export_not_found', message: 'Задание не найдено' } });
      if (job.state === 'preparing') {
        job.polls += 1;
        const step = Math.max(1, Math.ceil(job.total / 3));
        job.processed = Math.min(job.total, job.processed + step);
        if (job.processed >= job.total) {
          job.state = 'ready';
          job.archive = { name: 'Vanilla Dream Radio.zip', size: 12345678 };
        }
      }
      return json(res, 200, { job: exportJobPayload(job) });
    }

    const exportArchiveMatch = route.match(/^\/api\/admin\/exports\/([^/]+)\/archive$/);
    if (exportArchiveMatch && req.method === 'GET') {
      const job = stateMedia.exports.get(decodeURIComponent(exportArchiveMatch[1]));
      if (!job || job.state !== 'ready') {
        return json(res, 409, { detail: { code: 'archive_not_ready', message: 'Архив ещё не готов' } });
      }
      res.writeHead(200, {
        'Content-Type': 'application/zip',
        'Content-Disposition': 'attachment; filename="vanilla-dream-radio.zip"',
      });
      return res.end(Buffer.from('PK-MOCK-ARCHIVE'));
    }

    const exportCancelMatch = route.match(/^\/api\/admin\/exports\/([^/]+)\/cancel$/);
    if (exportCancelMatch && req.method === 'POST') {
      const job = stateMedia.exports.get(decodeURIComponent(exportCancelMatch[1]));
      if (!job) return json(res, 404, { detail: { code: 'export_not_found', message: 'Задание не найдено' } });
      job.state = 'cancelled';
      job.archive = null;
      return json(res, 200, { ok: true, job: exportJobPayload(job) });
    }

    return json(res, 200, { ok: true });
  }

  if (route === '/radio.mp3') {
    if (state.audioDelayMs > 0) {
      await new Promise((resolve) => setTimeout(resolve, state.audioDelayMs));
    }
    res.writeHead(200, { 'Content-Type': 'audio/mpeg' });
    return res.end();
  }
  if (route === '/radio.m3u') {
    res.writeHead(200, { 'Content-Type': 'audio/x-mpegurl' });
    return res.end(`${BASE}/radio.mp3\n`);
  }
  if (route === '/radio.pls') {
    res.writeHead(200, { 'Content-Type': 'audio/x-scpls' });
    return res.end(`[playlist]\nNumberOfEntries=1\nFile1=${BASE}/radio.mp3\n`);
  }

  const filePath = path.join(DIST, route === '/' ? 'index.html' : route);
  const resolved = path.resolve(filePath);
  if (resolved.startsWith(path.resolve(DIST)) && fs.existsSync(resolved) && fs.statSync(resolved).isFile()) {
    res.writeHead(200, { 'Content-Type': MIME[path.extname(resolved)] || 'application/octet-stream' });
    return fs.createReadStream(resolved).pipe(res);
  }
  res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
  return fs.createReadStream(path.join(DIST, 'index.html')).pipe(res);
});

server.listen(PORT, '127.0.0.1', () => {
  console.log(`mock server on ${BASE}`);
});
