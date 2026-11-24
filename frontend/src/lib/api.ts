import type {
  ConflictInfo,
  HistoryEntry,
  LoginResult,
  MeResult,
  NowPlaying,
  NowStation,
  NowTrack,
  Playlist,
  PlaylistDetail,
  QueueItem,
  QueueSnapshot,
  RadioLink,
  Rotation,
  RotationItem,
  RotationMode,
  ScheduleData,
  ScheduleRule,
  ScheduleRuleInput,
  ServiceInfo,
  Settings,
  StationStatus,
  StorageInfo,
  Track,
  TrackPage,
  TrackQuery,
} from './types';

/* ------------------------------------------------------------------ */
/* Типы-помощники для разбора ответов                                  */
/* ------------------------------------------------------------------ */

type JsonObject = Record<string, unknown>;

const isObject = (value: unknown): value is JsonObject =>
  typeof value === 'object' && value !== null && !Array.isArray(value);

const asString = (value: unknown, fallback = ''): string =>
  typeof value === 'string' ? value : typeof value === 'number' ? String(value) : fallback;

const asStringOrNull = (value: unknown): string | null => {
  if (typeof value === 'string' && value.length > 0) return value;
  if (typeof value === 'number' && Number.isFinite(value)) return String(value);
  return null;
};

const asNumber = (value: unknown): number | null =>
  typeof value === 'number' && Number.isFinite(value) ? value : null;

const asBoolean = (value: unknown, fallback = false): boolean => {
  if (typeof value === 'boolean') return value;
  if (typeof value === 'number') return value !== 0;
  if (typeof value === 'string') return value === '1' || value.toLowerCase() === 'true';
  return fallback;
};

const asArray = (value: unknown): unknown[] => (Array.isArray(value) ? value : []);

const pickArray = (raw: unknown, key: string): unknown[] => {
  if (Array.isArray(raw)) return raw;
  if (isObject(raw)) {
    const value = raw[key];
    if (Array.isArray(value)) return value;
    const nested = raw.items ?? raw.data ?? raw.results;
    if (Array.isArray(nested)) return nested;
  }
  return [];
};

const normalizeStatus = (value: unknown): StationStatus => {
  switch (value) {
    case 'off':
    case 'starting':
    case 'on_air':
    case 'no_music':
    case 'reconnecting':
    case 'error':
      return value;
    default:
      return 'unknown';
  }
};

/* ------------------------------------------------------------------ */
/* Ошибки и CSRF                                                       */
/* ------------------------------------------------------------------ */

const CSRF_HEADER = 'X-CSRF-Token';

let csrfToken: string | null = null;
let unauthorizedHandler: (() => void) | null = null;

export function setCsrfToken(token: string | null): void {
  csrfToken = token;
}

export function getCsrfToken(): string | null {
  return csrfToken;
}

export function onUnauthorized(handler: (() => void) | null): void {
  unauthorizedHandler = handler;
}

/** Уведомить приложение о 401 вне основного fetch-обёртки (например, из XHR-загрузки). */
export function handleUnauthorized(): void {
  unauthorizedHandler?.();
}

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;

  constructor(status: number, message: string, code = 'http_error') {
    super(message);
    this.name = 'ApiError';
    this.status = status;
    this.code = code;
  }
}

export function errorMessage(error: unknown): string {
  if (error instanceof ApiError) return error.message;
  if (error instanceof DOMException && error.name === 'AbortError') return 'Запрос отменён';
  if (error instanceof TypeError) return 'Нет связи с сервером. Проверьте подключение и повторите попытку.';
  if (error instanceof Error && error.message) return error.message;
  return 'Не удалось выполнить действие';
}

function statusFallback(status: number): string {
  switch (status) {
    case 400:
      return 'Некорректные данные запроса';
    case 401:
      return 'Требуется вход в панель управления';
    case 403:
      return 'Действие запрещено';
    case 404:
      return 'Не найдено';
    case 409:
      return 'Конфликт данных';
    case 413:
      return 'Файл слишком большой';
    case 422:
      return 'Данные не прошли проверку';
    case 429:
      return 'Слишком много попыток. Попробуйте позже';
    case 500:
      return 'Внутренняя ошибка сервера';
    case 502:
    case 503:
    case 504:
      return 'Сервис недоступен';
    default:
      return 'Не удалось выполнить запрос';
  }
}

async function toApiError(response: Response): Promise<ApiError> {
  let message = statusFallback(response.status);
  let code = `http_${response.status}`;
  try {
    const data: unknown = await response.json();
    if (isObject(data) && isObject(data.detail)) {
      const detail = data.detail;
      if (typeof detail.message === 'string' && detail.message.trim()) message = detail.message;
      if (typeof detail.code === 'string' && detail.code) code = detail.code;
    } else if (isObject(data) && typeof data.detail === 'string') {
      message = data.detail;
    }
  } catch {
    /* тело не JSON — оставляем сообщение по коду */
  }
  return new ApiError(response.status, message, code);
}

/* ------------------------------------------------------------------ */
/* Транспорт                                                           */
/* ------------------------------------------------------------------ */

type Method = 'GET' | 'POST' | 'PUT' | 'PATCH' | 'DELETE';

interface RequestOptions {
  method?: Method;
  body?: unknown;
  signal?: AbortSignal;
  query?: Record<string, string | number | boolean | null | undefined>;
}

function queryString(query: RequestOptions['query']): string {
  if (!query) return '';
  const params = new URLSearchParams();
  Object.entries(query).forEach(([key, value]) => {
    if (value === undefined || value === null || value === '') return;
    params.set(key, String(value));
  });
  const encoded = params.toString();
  return encoded ? `?${encoded}` : '';
}

async function request<T>(path: string, options: RequestOptions = {}): Promise<T> {
  const method = options.method ?? 'GET';
  const headers: Record<string, string> = { Accept: 'application/json' };
  let body: BodyInit | undefined;

  if (options.body instanceof FormData) {
    body = options.body;
  } else if (options.body !== undefined) {
    headers['Content-Type'] = 'application/json';
    body = JSON.stringify(options.body);
  }
  if (method !== 'GET' && csrfToken) headers[CSRF_HEADER] = csrfToken;

  let response: Response;
  try {
    response = await fetch(`${path}${queryString(options.query)}`, {
      method,
      headers,
      body,
      credentials: 'include',
      signal: options.signal,
    });
  } catch (error) {
    if (error instanceof DOMException && error.name === 'AbortError') throw error;
    throw new ApiError(0, 'Нет связи с сервером. Проверьте подключение и повторите попытку.', 'network');
  }

  if (response.status === 401 && !path.startsWith('/api/auth/login')) {
    unauthorizedHandler?.();
  }

  if (!response.ok) throw await toApiError(response);
  if (response.status === 204) return undefined as T;

  const contentType = response.headers.get('content-type') ?? '';
  if (!contentType.includes('application/json')) return (await response.text()) as unknown as T;
  return (await response.json()) as T;
}

/* ------------------------------------------------------------------ */
/* Нормализация ответов API                                            */
/* ------------------------------------------------------------------ */

function normalizeTrack(raw: unknown): Track | null {
  if (!isObject(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  return {
    id,
    title: asString(raw.title, 'Без названия'),
    artist: asStringOrNull(raw.artist),
    album: asStringOrNull(raw.album),
    genre: asStringOrNull(raw.genre),
    year: asNumber(raw.year),
    duration: asNumber(raw.duration),
    bitrate: asNumber(raw.bitrate),
    size: asNumber(raw.size),
    coverUrl: asStringOrNull(raw.cover_url),
    createdAt: asStringOrNull(raw.created_at),
    playlistsCount: asNumber(raw.playlists_count),
  };
}

function normalizeNowTrack(raw: unknown): NowTrack | null {
  if (!isObject(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  return {
    id,
    title: asString(raw.title, 'Без названия'),
    artist: asStringOrNull(raw.artist),
    album: asStringOrNull(raw.album),
    genre: asStringOrNull(raw.genre),
    year: asNumber(raw.year),
    coverUrl: asStringOrNull(raw.cover_url),
    duration: asNumber(raw.duration),
    startedAt: asStringOrNull(raw.started_at),
    elapsed: asNumber(raw.elapsed),
    progress: asNumber(raw.progress),
  };
}

function normalizeNowStation(raw: unknown): NowStation {
  const station = isObject(raw) ? raw : {};
  const desired = station.desired === 'playing' ? 'playing' : 'stopped';
  return {
    name: asString(station.name, 'Vanilla Dream Radio'),
    description: asStringOrNull(station.description),
    status: normalizeStatus(station.status),
    statusLabel: asStringOrNull(station.status_label),
    desired,
    streamUrl: asString(station.stream_url),
    m3uUrl: asString(station.m3u_url),
    plsUrl: asString(station.pls_url),
    baseUrl: asString(station.base_url) || window.location.origin,
    bitrate: asNumber(station.bitrate),
  };
}

export function normalizeNowPlaying(raw: unknown): NowPlaying {
  const data = isObject(raw) ? raw : {};
  const listeners = isObject(data.listeners) ? data.listeners : {};
  const nextSchedule = isObject(data.next_schedule_change) ? data.next_schedule_change : null;
  const nextTrackRaw = isObject(data.next_track) ? data.next_track : null;
  const sourceRaw = isObject(data.source) ? data.source : null;

  return {
    station: normalizeNowStation(data.station),
    track: normalizeNowTrack(data.track),
    source: sourceRaw
      ? {
          kind: asString(sourceRaw.kind),
          label: asString(sourceRaw.label, asString(sourceRaw.kind)),
          playlistId: asStringOrNull(sourceRaw.playlist_id),
          name: asStringOrNull(sourceRaw.name),
        }
      : null,
    listeners: {
      available: asBoolean(listeners.available),
      count: asNumber(listeners.count),
    },
    nextTrack: nextTrackRaw
      ? {
          id: asString(nextTrackRaw.id),
          title: asString(nextTrackRaw.title, 'Без названия'),
          artist: asStringOrNull(nextTrackRaw.artist),
          duration: asNumber(nextTrackRaw.duration),
        }
      : null,
    nextScheduleChange: nextSchedule
      ? { at: asString(nextSchedule.at), ruleName: asStringOrNull(nextSchedule.rule_name) }
      : null,
    statsAvailable: asBoolean(data.stats_available),
  };
}

function normalizePlaylist(raw: unknown): Playlist | null {
  if (!isObject(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  const repeat = raw.repeat_mode;
  return {
    id,
    name: asString(raw.name, 'Без названия'),
    description: asStringOrNull(raw.description),
    coverUrl: asStringOrNull(raw.cover_url),
    trackCount: asNumber(raw.track_count ?? raw.tracks_count),
    duration: asNumber(raw.duration ?? raw.total_duration),
    shuffle: asBoolean(raw.shuffle),
    repeatMode: repeat === 'all' || repeat === 'one' ? repeat : 'none',
    createdAt: asStringOrNull(raw.created_at),
    updatedAt: asStringOrNull(raw.updated_at),
  };
}

function minutesToClock(value: number | null): string | null {
  if (value === null) return null;
  const normalized = ((Math.trunc(value) % 1440) + 1440) % 1440;
  return `${String(Math.floor(normalized / 60)).padStart(2, '0')}:${String(normalized % 60).padStart(2, '0')}`;
}

function normalizeScheduleRule(raw: unknown): ScheduleRule | null {
  if (!isObject(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  const daysRaw = raw.days;
  let days: number[] = [];
  if (Array.isArray(daysRaw)) {
    days = daysRaw
      .map((day) => (typeof day === 'number' ? day : Number.parseInt(asString(day), 10)))
      .filter((day) => Number.isInteger(day) && day >= 0 && day <= 6);
  } else if (typeof daysRaw === 'string') {
    days = daysRaw
      .split(/[,\s]+/)
      .map((day) => Number.parseInt(day, 10))
      .filter((day) => Number.isInteger(day) && day >= 0 && day <= 6);
  }
  const start =
    asString(raw.start) || minutesToClock(asNumber(raw.start_min)) || '00:00';
  const end = asString(raw.end) || minutesToClock(asNumber(raw.end_min)) || '00:00';
  return {
    id,
    name: asString(raw.name, 'Без названия'),
    playlistId: asStringOrNull(raw.playlist_id),
    useRotation: asBoolean(raw.use_rotation),
    rotation: Math.max(0, Math.trunc(asNumber(raw.rotation) ?? 1)),
    days,
    start,
    end,
    enabled: asBoolean(raw.enabled, true),
    priority: Math.trunc(asNumber(raw.priority) ?? 0),
    transition: asString(raw.transition, 'after_track'),
  };
}

function normalizeRotationItem(raw: unknown, index: number): RotationItem | null {
  if (!isObject(raw)) return null;
  const nestedPlaylist = isObject(raw.playlist) ? raw.playlist : null;
  const playlistId = asString(raw.playlist_id ?? nestedPlaylist?.id);
  if (!playlistId) return null;
  return {
    id: asStringOrNull(raw.id),
    playlistId,
    playlistName: asStringOrNull(raw.playlist_name ?? nestedPlaylist?.name),
    enabled: asBoolean(raw.enabled, true),
    trackCount: Math.max(1, Math.trunc(asNumber(raw.track_count) ?? 1)),
    position: Math.trunc(asNumber(raw.position) ?? index),
  };
}

function normalizeQueueItem(raw: unknown): QueueItem | null {
  if (!isObject(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  const source = isObject(raw.source) ? raw.source : null;
  const sourceText = typeof raw.source === 'string' ? raw.source : null;
  return {
    id,
    track: normalizeTrack(raw.track),
    sourceKind: source
      ? asStringOrNull(source.kind)
      : sourceText ?? asStringOrNull(raw.source_kind),
    sourceLabel: source
      ? asStringOrNull(source.label ?? source.name)
      : asStringOrNull(raw.source_label) ?? sourceText,
    prepared: asBoolean(raw.prepared),
    manual: asBoolean(raw.manual),
    estimatedStart: asStringOrNull(raw.estimated_start),
  };
}

const SERVICE_NAMES: Record<string, string> = {
  icecast: 'Icecast',
  liquidsoap: 'Liquidsoap',
  api: 'Backend',
  backend: 'Backend',
  station: 'Движок',
};

function normalizeService(raw: unknown): ServiceInfo | null {
  if (!isObject(raw)) return null;
  const id = asString(raw.id ?? raw.key ?? raw.name);
  if (!id) return null;
  const stateRaw = asString(raw.state ?? raw.status ?? raw.active);
  let state: ServiceInfo['state'] = 'unknown';
  if (stateRaw === 'active' || stateRaw === 'running' || stateRaw === 'on') state = 'active';
  else if (stateRaw === 'inactive' || stateRaw === 'stopped' || stateRaw === 'off' || stateRaw === 'false') state = 'inactive';
  else if (stateRaw === 'failed' || stateRaw === 'error') state = 'failed';
  else if (asBoolean(raw.active)) state = 'active';
  return {
    id,
    name: asString(raw.label ?? raw.name, SERVICE_NAMES[id] ?? id),
    state,
    lastStart: asStringOrNull(raw.last_start ?? raw.started_at),
    lastError: asStringOrNull(raw.last_error ?? raw.error),
  };
}

function normalizeHistoryEntry(raw: unknown): HistoryEntry | null {
  if (!isObject(raw)) return null;
  const title = asString(raw.title);
  if (!title) return null;
  return {
    id: asStringOrNull(raw.id),
    trackId: asStringOrNull(raw.track_id),
    title,
    artist: asStringOrNull(raw.artist),
    album: asStringOrNull(raw.album),
    startedAt: asStringOrNull(raw.started_at),
    endedAt: asStringOrNull(raw.ended_at),
    sourceKind: asStringOrNull(raw.source_kind),
    sourceRef: asStringOrNull(raw.source_ref),
  };
}

export function normalizeSettings(raw: unknown): Settings {
  const data = isObject(raw) && isObject(raw.settings) ? raw.settings : isObject(raw) ? raw : {};
  const desired = data.desired_state === 'playing' ? 'playing' : 'stopped';
  return {
    stationName: asString(data.station_name, 'Vanilla Dream Radio'),
    stationDescription: asString(data.station_description, ''),
    stationGenre: asString(data.station_genre, ''),
    bitrate: Math.trunc(asNumber(data.bitrate) ?? 128),
    timezone: asString(data.timezone, 'UTC'),
    scheduleEnabled: asBoolean(data.schedule_enabled, true),
    defaultTransition: asString(data.default_transition, 'after_track'),
    defaultPlaylistId: asString(data.default_playlist_id, ''),
    queueDepth: Math.trunc(asNumber(data.queue_depth) ?? 6),
    maxUploadMb: Math.trunc(asNumber(data.max_upload_mb) ?? 300),
    maxLibraryGb: Math.trunc(asNumber(data.max_library_gb) ?? 0),
    desiredState: desired,
    engineLastStart: asString(data.engine_last_start, ''),
    engineLastError: asString(data.engine_last_error, ''),
  };
}

export function normalizeStorage(raw: unknown): StorageInfo {
  const data = isObject(raw) ? raw : {};
  const tracksObj = isObject(data.tracks) ? data.tracks : {};
  const libraryObj = isObject(data.library) ? data.library : {};
  return {
    tracks: Math.trunc(asNumber(tracksObj.count ?? data.track_count ?? libraryObj.count) ?? 0),
    libraryBytes: asNumber(tracksObj.bytes ?? data.library_bytes ?? libraryObj.bytes ?? data.used_bytes) ?? 0,
    freeBytes: asNumber(data.free_bytes ?? data.free ?? data.disk_free_bytes),
    totalBytes: asNumber(data.total_bytes ?? data.total ?? data.disk_total_bytes),
  };
}

/* ------------------------------------------------------------------ */
/* Ссылки на трансляцию                                                */
/* ------------------------------------------------------------------ */

const LINK_META: Record<string, { label: string; hint: string | null; primary?: boolean }> = {
  world_radio: {
    label: 'Minecraft / World Radio',
    hint: 'Прямая ссылка без HTML-плеера — основной адрес для World Radio',
    primary: true,
  },
  mp3: { label: 'Прямой MP3', hint: 'HTTPS-поток, формат MP3' },
  http_mp3: { label: 'HTTP MP3', hint: 'Без TLS — только для плееров без поддержки HTTPS' },
  m3u: { label: 'M3U-плейлист', hint: 'Для VLC и мобильных плееров' },
  pls: { label: 'PLS-плейлист', hint: 'Формат PLS' },
  player: { label: 'Веб-плеер', hint: 'Публичная страница станции' },
  admin: { label: 'Панель управления', hint: 'Вход для администратора' },
};

const LINK_ORDER = ['world_radio', 'mp3', 'http_mp3', 'player', 'm3u', 'pls', 'admin'];

function normalizeLinkEntry(key: string, value: unknown): RadioLink | null {
  let url = '';
  let label = '';
  let hint: string | null = null;
  if (typeof value === 'string') {
    url = value;
  } else if (isObject(value)) {
    url = asString(value.url ?? value.href);
    label = asString(value.label ?? value.title ?? value.name);
    hint = asStringOrNull(value.hint ?? value.description);
  }
  if (!url) return null;
  const meta = LINK_META[key];
  return {
    id: key,
    label: label || meta?.label || key,
    url,
    hint: hint ?? meta?.hint ?? null,
    primary: meta?.primary ?? false,
  };
}

export function normalizeLinks(raw: unknown): RadioLink[] {
  const entries: Array<[string, unknown]> = [];
  const source = isObject(raw) && (isObject(raw.links) || Array.isArray(raw.links)) ? raw.links : raw;

  if (Array.isArray(source)) {
    source.forEach((item, index) => {
      if (isObject(item)) {
        const key = asString(item.id ?? item.key ?? item.kind, `link_${index}`);
        entries.push([key, item]);
      }
    });
  } else if (isObject(source)) {
    Object.entries(source).forEach(([key, value]) => {
      if (key === 'base_url' || key === 'station') return;
      entries.push([key, value]);
    });
  }

  const links: RadioLink[] = [];
  const seen = new Set<string>();
  entries.forEach(([key, value]) => {
    const link = normalizeLinkEntry(key, value);
    if (!link || seen.has(link.id)) return;
    seen.add(link.id);
    links.push(link);
  });

  const rank = (id: string) => {
    const index = LINK_ORDER.indexOf(id);
    return index === -1 ? LINK_ORDER.length : index;
  };
  return links.sort((a, b) => rank(a.id) - rank(b.id));
}

/** Резервный набор ссылок из публичного now-playing (если /api/admin/links недоступен). */
export function linksFromStation(station: NowStation): RadioLink[] {
  const links: RadioLink[] = [];
  if (station.streamUrl) {
    links.push({
      id: 'world_radio',
      label: LINK_META.world_radio.label,
      url: station.streamUrl,
      hint: LINK_META.world_radio.hint,
      primary: true,
    });
    links.push({
      id: 'mp3',
      label: LINK_META.mp3.label,
      url: station.streamUrl,
      hint: LINK_META.mp3.hint,
      primary: false,
    });
  }
  if (station.baseUrl) {
    links.push({
      id: 'player',
      label: LINK_META.player.label,
      url: new URL('/', ensureBase(station.baseUrl)).toString(),
      hint: LINK_META.player.hint,
      primary: false,
    });
  }
  if (station.m3uUrl) {
    links.push({ id: 'm3u', label: LINK_META.m3u.label, url: station.m3uUrl, hint: LINK_META.m3u.hint, primary: false });
  }
  if (station.plsUrl) {
    links.push({ id: 'pls', label: LINK_META.pls.label, url: station.plsUrl, hint: LINK_META.pls.hint, primary: false });
  }
  if (station.baseUrl) {
    links.push({
      id: 'admin',
      label: LINK_META.admin.label,
      url: new URL('/admin', ensureBase(station.baseUrl)).toString(),
      hint: LINK_META.admin.hint,
      primary: false,
    });
  }
  return links;
}

function ensureBase(base: string): string {
  try {
    const url = new URL(base);
    return url.toString();
  } catch {
    return window.location.origin;
  }
}

/* ------------------------------------------------------------------ */
/* Публичные методы                                                    */
/* ------------------------------------------------------------------ */

async function publicNowPlaying(): Promise<NowPlaying> {
  return normalizeNowPlaying(await request<unknown>('/api/public/now-playing'));
}

async function publicHistory(limit = 10): Promise<HistoryEntry[]> {
  const raw = await request<unknown>('/api/public/history', { query: { limit } });
  return pickArray(raw, 'items')
    .map(normalizeHistoryEntry)
    .filter((entry): entry is HistoryEntry => entry !== null);
}

/* ------------------------------------------------------------------ */
/* Авторизация                                                         */
/* ------------------------------------------------------------------ */

async function authMe(): Promise<MeResult> {
  const raw = await request<unknown>('/api/auth/me');
  const data = isObject(raw) ? raw : {};
  const token = asStringOrNull(data.csrf_token);
  return { authenticated: asBoolean(data.authenticated), csrfToken: token };
}

async function authLogin(password: string): Promise<LoginResult> {
  const raw = await request<unknown>('/api/auth/login', { method: 'POST', body: { password } });
  const data = isObject(raw) ? raw : {};
  return { ok: asBoolean(data.ok, true), csrfToken: asStringOrNull(data.csrf_token) };
}

async function authLogout(): Promise<void> {
  await request<unknown>('/api/auth/logout', { method: 'POST' });
}

async function authChangePassword(current: string, next: string): Promise<void> {
  await request<unknown>('/api/auth/password', { method: 'POST', body: { current, new: next } });
}

/* ------------------------------------------------------------------ */
/* Медиатека                                                           */
/* ------------------------------------------------------------------ */

async function tracksList(query: TrackQuery = {}): Promise<TrackPage> {
  const raw = await request<unknown>('/api/admin/tracks', {
    query: {
      q: query.q,
      sort: query.sort,
      order: query.order,
      limit: query.limit,
      offset: query.offset,
    },
  });
  const items = pickArray(raw, 'items')
    .map(normalizeTrack)
    .filter((track): track is Track => track !== null);
  const data = isObject(raw) ? raw : {};
  const libraryObj = isObject(data.library) ? data.library : {};
  return {
    items,
    total: Math.trunc(asNumber(data.total) ?? items.length),
    library: {
      count: Math.trunc(asNumber(libraryObj.count ?? data.total) ?? items.length),
      bytes: asNumber(libraryObj.bytes) ?? 0,
    },
  };
}

async function trackUpdate(id: string, patch: Record<string, unknown>): Promise<void> {
  await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}`, { method: 'PATCH', body: patch });
}

interface DeleteTrackResult {
  deleted: boolean;
  playlistsCount: number | null;
  pending: boolean;
}

async function trackDelete(id: string): Promise<DeleteTrackResult> {
  const raw = await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}`, { method: 'DELETE' });
  const data = isObject(raw) ? raw : {};
  return {
    deleted: asBoolean(data.deleted, true),
    playlistsCount: asNumber(data.playlists_count),
    pending: asBoolean(data.pending),
  };
}

async function tracksBulkDelete(ids: string[]): Promise<void> {
  await request<unknown>('/api/admin/tracks/bulk-delete', { method: 'POST', body: { ids } });
}

async function trackPlayNow(id: string): Promise<void> {
  await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}/play-now`, { method: 'POST' });
}

async function trackPlayNext(id: string): Promise<void> {
  await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}/play-next`, { method: 'POST' });
}

async function trackEnqueue(id: string): Promise<void> {
  await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}/queue`, { method: 'POST' });
}

/* ------------------------------------------------------------------ */
/* Плейлисты и ротация                                                 */
/* ------------------------------------------------------------------ */

async function playlistsList(): Promise<Playlist[]> {
  const raw = await request<unknown>('/api/admin/playlists');
  return pickArray(raw, 'items')
    .map(normalizePlaylist)
    .filter((playlist): playlist is Playlist => playlist !== null);
}

async function playlistGet(id: string): Promise<PlaylistDetail> {
  const raw = await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}`);
  const data = isObject(raw) ? raw : {};
  const playlistRaw = isObject(data.playlist) ? data.playlist : data;
  const playlist = normalizePlaylist(playlistRaw);
  if (!playlist) throw new ApiError(404, 'Плейлист не найден');
  const tracks = pickArray(data.tracks ?? raw, 'tracks')
    .map((item) => normalizeTrack(isObject(item) && isObject(item.track) ? item.track : item))
    .filter((track): track is Track => track !== null);
  return { playlist, tracks };
}

async function playlistCreate(payload: { name: string; description?: string }): Promise<Playlist> {
  const raw = await request<unknown>('/api/admin/playlists', { method: 'POST', body: payload });
  const data = isObject(raw) && isObject(raw.playlist) ? raw.playlist : raw;
  const playlist = normalizePlaylist(data);
  if (!playlist) throw new ApiError(500, 'Сервер не вернул созданный плейлист');
  return playlist;
}

async function playlistUpdate(id: string, patch: Record<string, unknown>): Promise<void> {
  await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}`, { method: 'PATCH', body: patch });
}

async function playlistDelete(id: string): Promise<void> {
  await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

async function playlistAddTracks(id: string, trackIds: string[]): Promise<void> {
  await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/tracks`, {
    method: 'POST',
    body: { track_ids: trackIds },
  });
}

async function playlistRemoveTrack(id: string, trackId: string): Promise<void> {
  await request<unknown>(
    `/api/admin/playlists/${encodeURIComponent(id)}/tracks/${encodeURIComponent(trackId)}`,
    { method: 'DELETE' },
  );
}

async function playlistReorder(id: string, trackIds: string[]): Promise<void> {
  await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/order`, {
    method: 'PUT',
    body: { track_ids: trackIds },
  });
}

async function playlistPlay(id: string): Promise<void> {
  await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/play`, { method: 'POST' });
}

async function playlistToRotation(id: string): Promise<void> {
  await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/rotation`, { method: 'POST' });
}

async function rotationGet(): Promise<Rotation> {
  const raw = await request<unknown>('/api/admin/rotation');
  const data = isObject(raw) ? raw : {};
  const modeRaw = asString(data.mode);
  const mode: RotationMode =
    modeRaw === 'by_count' || modeRaw === 'random' ? modeRaw : 'sequential';
  const items = pickArray(raw, 'items')
    .map((item, index) => normalizeRotationItem(item, index))
    .filter((item): item is RotationItem => item !== null)
    .sort((a, b) => a.position - b.position);
  return { mode, items };
}

async function rotationSave(payload: { mode: RotationMode; items: Array<Record<string, unknown>> }): Promise<void> {
  await request<unknown>('/api/admin/rotation', { method: 'PUT', body: payload });
}

interface RotationPreview {
  playlistName: string | null;
  playlistId: string | null;
  mode: string | null;
}

async function rotationPreview(): Promise<RotationPreview> {
  const raw = await request<unknown>('/api/admin/rotation/next-preview');
  const data = isObject(raw) ? raw : {};
  const playlist = isObject(data.playlist) ? data.playlist : data;
  return {
    playlistName: asStringOrNull(playlist.name ?? data.playlist_name),
    playlistId: asStringOrNull(playlist.id ?? data.playlist_id),
    mode: asStringOrNull(data.mode),
  };
}

/* ------------------------------------------------------------------ */
/* Расписание                                                          */
/* ------------------------------------------------------------------ */

async function scheduleGet(): Promise<ScheduleData> {
  const raw = await request<unknown>('/api/admin/schedule');
  const data = isObject(raw) ? raw : {};
  const rules = pickArray(raw, 'rules')
    .map(normalizeScheduleRule)
    .filter((rule): rule is ScheduleRule => rule !== null);
  return {
    rules,
    timezone: asStringOrNull(data.timezone),
    enabled: 'schedule_enabled' in data ? asBoolean(data.schedule_enabled) : null,
  };
}

async function scheduleCreate(payload: ScheduleRuleInput): Promise<void> {
  await request<unknown>('/api/admin/schedule', { method: 'POST', body: payload });
}

async function scheduleUpdate(id: string, payload: Partial<ScheduleRuleInput>): Promise<void> {
  await request<unknown>(`/api/admin/schedule/${encodeURIComponent(id)}`, { method: 'PATCH', body: payload });
}

async function scheduleDelete(id: string): Promise<void> {
  await request<unknown>(`/api/admin/schedule/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

async function scheduleConflicts(): Promise<ConflictInfo[]> {
  const raw = await request<unknown>('/api/admin/schedule/conflicts');
  const list = pickArray(raw, 'conflicts');
  return list
    .map((entry): ConflictInfo | null => {
      if (isObject(entry)) {
        const ids = asArray(entry.rule_ids ?? entry.ids)
          .map((id) => asString(id))
          .filter(Boolean);
        const message = asString(entry.message);
        if (ids.length === 0 && !message) return null;
        return { ruleIds: ids, message: message || 'Правила пересекаются' };
      }
      return null;
    })
    .filter((entry): entry is ConflictInfo => entry !== null);
}

/* ------------------------------------------------------------------ */
/* Очередь                                                             */
/* ------------------------------------------------------------------ */

async function queueGet(): Promise<QueueSnapshot> {
  const raw = await request<unknown>('/api/admin/queue');
  const data = isObject(raw) ? raw : {};
  const items = pickArray(raw, 'items')
    .map(normalizeQueueItem)
    .filter((item): item is QueueItem => item !== null);
  return {
    current: normalizeQueueItem(data.current),
    items,
    depth: asNumber(data.depth),
  };
}

async function queueMove(id: string, moveTo: number): Promise<void> {
  await request<unknown>(`/api/admin/queue/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: { move_to: moveTo },
  });
}

async function queueRemove(id: string): Promise<void> {
  await request<unknown>(`/api/admin/queue/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

async function queueShuffle(): Promise<void> {
  await request<unknown>('/api/admin/queue/shuffle', { method: 'POST' });
}

async function queueClearManual(): Promise<void> {
  await request<unknown>('/api/admin/queue/clear-manual', { method: 'POST' });
}

/* ------------------------------------------------------------------ */
/* Станция                                                             */
/* ------------------------------------------------------------------ */

async function stationStatus(): Promise<NowPlaying> {
  return normalizeNowPlaying(await request<unknown>('/api/admin/station/status'));
}

async function stationAction(action: 'start' | 'stop' | 'restart' | 'skip'): Promise<void> {
  await request<unknown>(`/api/admin/station/${action}`, { method: 'POST' });
}

/* ------------------------------------------------------------------ */
/* Настройки, хранилище, службы, ссылки, история                       */
/* ------------------------------------------------------------------ */

async function settingsGet(): Promise<Settings> {
  return normalizeSettings(await request<unknown>('/api/admin/settings'));
}

async function settingsSave(patch: Record<string, unknown>): Promise<Settings> {
  const raw = await request<unknown>('/api/admin/settings', { method: 'PUT', body: patch });
  return normalizeSettings(raw);
}

async function storageGet(): Promise<StorageInfo> {
  return normalizeStorage(await request<unknown>('/api/admin/storage'));
}

async function servicesGet(): Promise<ServiceInfo[]> {
  const raw = await request<unknown>('/api/admin/services');
  if (Array.isArray(raw)) {
    return raw.map(normalizeService).filter((service): service is ServiceInfo => service !== null);
  }
  if (isObject(raw)) {
    const list = raw.services;
    if (Array.isArray(list)) {
      return list.map(normalizeService).filter((service): service is ServiceInfo => service !== null);
    }
    if (isObject(list)) {
      return Object.entries(list)
        .map(([id, value]) => {
          if (isObject(value)) return normalizeService({ id, ...value });
          return normalizeService({ id, state: value });
        })
        .filter((service): service is ServiceInfo => service !== null);
    }
  }
  return pickArray(raw, 'items')
    .map(normalizeService)
    .filter((service): service is ServiceInfo => service !== null);
}

async function linksGet(): Promise<RadioLink[]> {
  return normalizeLinks(await request<unknown>('/api/admin/links'));
}

async function adminHistory(limit = 20): Promise<HistoryEntry[]> {
  const raw = await request<unknown>('/api/admin/history', { query: { limit } });
  return pickArray(raw, 'items')
    .map(normalizeHistoryEntry)
    .filter((entry): entry is HistoryEntry => entry !== null);
}

/* ------------------------------------------------------------------ */
/* Публичный API                                                       */
/* ------------------------------------------------------------------ */

export const api = {
  auth: {
    me: authMe,
    login: authLogin,
    logout: authLogout,
    changePassword: authChangePassword,
  },
  public: {
    nowPlaying: publicNowPlaying,
    history: publicHistory,
  },
  admin: {
    tracks: {
      list: tracksList,
      update: trackUpdate,
      remove: trackDelete,
      bulkDelete: tracksBulkDelete,
      playNow: trackPlayNow,
      playNext: trackPlayNext,
      enqueue: trackEnqueue,
    },
    playlists: {
      list: playlistsList,
      get: playlistGet,
      create: playlistCreate,
      update: playlistUpdate,
      remove: playlistDelete,
      addTracks: playlistAddTracks,
      removeTrack: playlistRemoveTrack,
      reorder: playlistReorder,
      play: playlistPlay,
      toRotation: playlistToRotation,
    },
    rotation: {
      get: rotationGet,
      save: rotationSave,
      preview: rotationPreview,
    },
    schedule: {
      get: scheduleGet,
      create: scheduleCreate,
      update: scheduleUpdate,
      remove: scheduleDelete,
      conflicts: scheduleConflicts,
    },
    queue: {
      get: queueGet,
      move: queueMove,
      remove: queueRemove,
      shuffle: queueShuffle,
      clearManual: queueClearManual,
    },
    station: {
      status: stationStatus,
      start: () => stationAction('start'),
      stop: () => stationAction('stop'),
      restart: () => stationAction('restart'),
      skip: () => stationAction('skip'),
    },
    settings: {
      get: settingsGet,
      save: settingsSave,
    },
    storage: storageGet,
    services: servicesGet,
    links: linksGet,
    history: adminHistory,
  },
};
