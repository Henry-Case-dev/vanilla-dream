import type {
  AddTracksResult,
  ApplicationState,
  BulkDeleteResult,
  ConflictInfo,
  DecisionAction,
  DeleteMode,
  DeleteTrackResult,
  DuplicateKind,
  EffectiveSource,
  OverrideInfo,
  PendingSource,
  PlaybackMode,
  PlaylistUpdateResult,
  RepeatGovernedBy,
  StationModeResult,
  ExportCreateResult,
  ExportFile,
  ExportJob,
  ExportJobState,
  ExportRequest,
  HistoryEntry,
  LoginResult,
  MeResult,
  NowPlaying,
  NowStation,
  NowTrack,
  PendingMatch,
  PendingUpload,
  Playlist,
  PlaylistDetail,
  PlaylistPlayResult,
  QueueItem,
  QueueSnapshot,
  RadioLink,
  RemoveTrackResult,
  Rotation,
  RotationItem,
  RotationMode,
  ScheduleData,
  ScheduleRule,
  ScheduleRuleInput,
  ServiceInfo,
  Settings,
  StationState,
  StationStatus,
  StorageInfo,
  Track,
  TrackComparison,
  TrackCoverKind,
  TrackIdPage,
  TrackPage,
  TrackQuery,
  UploadBatch,
  UploadBatchCounts,
  UploadBatchItem,
  UploadBatchStatus,
  UploadItemState,
  UploadResponse,
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

function requireObject(value: unknown, what: string): JsonObject {
  if (!isObject(value)) throw contractError(what);
  return value;
}

function requireString(value: unknown, what: string): string {
  if (typeof value !== 'string' || value.length === 0) throw contractError(what);
  return value;
}

function requireNumber(value: unknown, what: string): number {
  const number = asNumber(value);
  if (number === null) throw contractError(what);
  return number;
}

function requireArray(value: unknown, what: string): unknown[] {
  if (!Array.isArray(value)) throw contractError(what);
  return value;
}

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

/** Ответ сервера не соответствует зафиксированному контракту. */
export class PayloadError extends Error {
  constructor(message: string) {
    super(message);
    this.name = 'PayloadError';
  }
}

export function contractError(what: string): PayloadError {
  return new PayloadError(`Некорректный ответ сервера: ${what}. Обновите страницу и повторите.`);
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

function normalizeCoverKind(value: unknown, coverUrl: string | null): TrackCoverKind {
  if (value === 'custom' || value === 'file' || value === 'none') return value;
  // Старый backend не присылал cover_kind: выводим из наличия обложки.
  return coverUrl ? 'file' : 'none';
}

const DUPLICATE_KINDS: readonly DuplicateKind[] = ['content', 'tags', 'possible'];

function normalizeDuplicateKind(value: unknown): DuplicateKind | null {
  return DUPLICATE_KINDS.includes(value as DuplicateKind) ? (value as DuplicateKind) : null;
}

function normalizeTrack(raw: unknown): Track | null {
  if (!isObject(raw)) return null;
  const id = asString(raw.id);
  if (!id) return null;
  const coverUrl = asStringOrNull(raw.cover_url);
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
    ext: asStringOrNull(raw.ext),
    coverUrl,
    coverKind: normalizeCoverKind(raw.cover_kind, coverUrl),
    isDuplicate: asBoolean(raw.is_duplicate),
    duplicateKind: normalizeDuplicateKind(raw.duplicate_kind),
    duplicateOf: asStringOrNull(raw.duplicate_of),
    canRevertCover: asBoolean(raw.can_revert_cover),
    originalCoverUrl: asStringOrNull(
      raw.original_cover_url ?? raw.source_cover_url ?? raw.extracted_cover_url,
    ),
    sourceFilename: asStringOrNull(raw.source_filename),
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

function normalizePlaybackMode(value: unknown, fallback: PlaybackMode = 'playlist'): PlaybackMode {
  return value === 'auto' || value === 'playlist' ? value : fallback;
}

function normalizeEffectiveSource(raw: unknown): EffectiveSource | null {
  if (!isObject(raw)) return null;
  const kind = asString(raw.kind);
  if (!kind) return null;
  return {
    kind,
    playlistId: asStringOrNull(raw.playlist_id ?? raw.playlistId),
    name: asStringOrNull(raw.name),
    reason: asStringOrNull(raw.reason),
  };
}

function normalizeRepeatGovernedBy(value: unknown): RepeatGovernedBy {
  return value === 'playlist' || value === 'rotation' ? value : null;
}

function normalizeApplication(value: unknown): ApplicationState {
  return value === 'applied' || value === 'pending' ? value : 'saved';
}

function normalizePendingSource(raw: unknown): PendingSource | null {
  if (!isObject(raw)) return null;
  const playlistId = asStringOrNull(raw.playlist_id ?? raw.playlistId);
  if (!playlistId) return null;
  return {
    playlistId,
    mode: normalizePlaybackMode(raw.mode),
    requestedAt: asStringOrNull(raw.requested_at ?? raw.requestedAt),
  };
}

function normalizeOverride(raw: unknown): OverrideInfo | null {
  if (!isObject(raw)) return null;
  return {
    id: asString(raw.id),
    kind: asString(raw.kind) || 'override',
    playlistId: asStringOrNull(raw.playlist_id ?? raw.playlistId),
    playlistName: asStringOrNull(raw.playlist_name ?? raw.playlistName),
    scope: asString(raw.scope),
    expiresAt: asStringOrNull(raw.expires_at ?? raw.expiresAt),
    active: asBoolean(raw.active, true),
    createdAt: asStringOrNull(raw.created_at ?? raw.createdAt),
    note: asStringOrNull(raw.note),
  };
}

export function normalizeStationState(raw: unknown): StationState {
  const data = requireObject(raw, 'состояние станции');
  const state = isObject(data.state) ? data.state : data;
  const status = normalizeStatus(state.status);
  return {
    mode: normalizePlaybackMode(state.mode ?? state.playback_mode),
    selectedPlaylistId: asStringOrNull(state.selected_playlist_id),
    programPlaylistId: asStringOrNull(state.program_playlist_id),
    effectiveSource: normalizeEffectiveSource(
      state.effective_source ?? state.effectiveSource ?? state.source,
    ),
    pendingSource: normalizePendingSource(state.pending_source ?? state.pendingSource),
    override: normalizeOverride(state.override),
    desired: state.desired === 'stopped' ? 'stopped' : 'playing',
    status: status === 'unknown' ? 'unknown' : status,
    reason: asStringOrNull(state.reason),
    repeatGovernedBy: normalizeRepeatGovernedBy(state.repeat_governed_by),
    engineInstance: asStringOrNull(state.engine_instance),
    revision: asNumber(state.state_revision ?? state.revision) ?? 0,
    appliedRevision: asNumber(state.applied_revision),
    notice: asStringOrNull(state.notice),
    diagnosticReason: asStringOrNull(state.diagnostic_reason),
  };
}

function normalizeNowStation(raw: unknown): NowStation {
  const station = requireObject(raw, 'станция в now-playing');
  const status = normalizeStatus(station.status);
  if (status === 'unknown') throw contractError('неизвестный статус станции');
  const desired = station.desired === 'playing' ? 'playing' : 'stopped';
  const effectiveSource = normalizeEffectiveSource(
    station.effective_source ?? station.effectiveSource ?? station.source,
  );
  return {
    name: asString(station.name, 'Vanilla Dream Radio'),
    description: asStringOrNull(station.description),
    status,
    statusLabel: asStringOrNull(station.status_label),
    reason: asStringOrNull(station.reason),
    desired,
    streamUrl: asString(station.stream_url),
    m3uUrl: asString(station.m3u_url),
    plsUrl: asString(station.pls_url),
    baseUrl: asString(station.base_url) || window.location.origin,
    bitrate: asNumber(station.bitrate),
    mode: normalizePlaybackMode(station.mode ?? station.playback_mode),
    programPlaylistId: asStringOrNull(station.program_playlist_id),
    effectiveSource,
    pendingSource: normalizePendingSource(station.pending_source ?? station.pendingSource),
    override: normalizeOverride(station.override),
    repeatGovernedBy: normalizeRepeatGovernedBy(station.repeat_governed_by),
    revision: asNumber(station.state_revision ?? station.revision) ?? 0,
    notice: asStringOrNull(station.notice),
    diagnosticReason: asStringOrNull(station.diagnostic_reason),
  };
}

export function normalizeNowPlaying(raw: unknown): NowPlaying {
  const data = requireObject(raw, 'now-playing');
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
    queueRevision: asNumber(data.queue_revision),
    historyRevision: asNumber(data.history_revision),
  };
}

export const SYSTEM_PLAYLIST_ID = 'library';
export const SYSTEM_PLAYLIST_NAME = 'Вся медиатека';
export const SYSTEM_PLAYLIST_DESCRIPTION = 'Все треки медиатеки. Обновляется автоматически.';

export function normalizePlaylist(raw: unknown): Playlist {
  const data = requireObject(raw, 'плейлист');
  const id = requireString(data.id, 'id плейлиста');
  const system = id === SYSTEM_PLAYLIST_ID || asBoolean(data.system);
  const repeat = data.repeat_mode;
  const coverUrl = asStringOrNull(data.cover_url);
  const coverKindRaw = asString(data.cover_kind);
  const coverKind =
    coverKindRaw === 'custom' || coverKindRaw === 'auto' || coverKindRaw === 'none'
      ? coverKindRaw
      : coverUrl
        ? 'auto'
        : 'none';
  return {
    id,
    name: system ? SYSTEM_PLAYLIST_NAME : requireString(data.name, 'название плейлиста'),
    description: system
      ? asStringOrNull(data.description) ?? SYSTEM_PLAYLIST_DESCRIPTION
      : asStringOrNull(data.description),
    system,
    coverUrl,
    coverKind,
    trackCount: requireNumber(data.track_count, 'track_count плейлиста'),
    duration: requireNumber(data.duration, 'duration плейлиста'),
    shuffle: asBoolean(data.shuffle),
    repeatMode: repeat === 'all' || repeat === 'one' ? repeat : 'none',
    createdAt: asStringOrNull(data.created_at),
    updatedAt: asStringOrNull(data.updated_at),
    pendingDeleteCount: asNumber(data.pending_delete_count),
    repeatGovernedBy: normalizeRepeatGovernedBy(data.repeat_governed_by),
  };
}

export function normalizePlaylistDetail(raw: unknown): PlaylistDetail {
  const data = requireObject(raw, 'деталь плейлиста');
  const playlistRaw = requireObject(data.playlist, 'playlist в детали плейлиста');
  const playlist = normalizePlaylist(playlistRaw);
  const rawTracks = playlistRaw.tracks ?? data.tracks;
  if (!Array.isArray(rawTracks)) throw contractError('треки в детали плейлиста');
  const tracks = rawTracks.map((item) => {
    const track = normalizeTrack(isObject(item) && isObject(item.track) ? item.track : item);
    if (!track) throw contractError('трек в детали плейлиста');
    return track;
  });
  const tracksTotal = asNumber(playlistRaw.tracks_total ?? data.tracks_total) ?? tracks.length;
  return { playlist, tracks, tracksTotal };
}

export function normalizeAddTracksResult(raw: unknown): AddTracksResult {
  const data = requireObject(raw, 'результат добавления треков');
  return {
    added: requireNumber(data.added, 'added'),
    skipped: requireNumber(data.skipped, 'skipped'),
    unavailable: requireNumber(data.unavailable, 'unavailable'),
    playlist: normalizePlaylist(data.playlist),
    application: 'application' in data ? normalizeApplication(data.application) : null,
    inCurrentCycle: asBoolean(data.in_current_cycle),
    engineSynced: 'engine_synced' in data ? asBoolean(data.engine_synced) : null,
    stateRevision: asNumber(data.state_revision),
    appliedRevision: asNumber(data.applied_revision),
  };
}

export function normalizeRemoveTrackResult(raw: unknown): RemoveTrackResult {
  const data = requireObject(raw, 'результат удаления трека');
  return {
    removed: asBoolean(data.removed),
    playlist: normalizePlaylist(data.playlist),
  };
}

export function normalizeTrackIds(raw: unknown): TrackIdPage {
  const data = requireObject(raw, 'список id треков');
  const ids = requireArray(data.ids, 'ids').map((id) => requireString(id, 'id трека'));
  return {
    ids: Array.from(new Set(ids)),
    total: requireNumber(data.total, 'total id треков'),
  };
}

/* ------------------------------------------------------------------ */
/* Загрузка музыки (VD-RADIO-MEDIA-02)                                 */
/* ------------------------------------------------------------------ */

function normalizeComparison(raw: unknown, what: string): TrackComparison {
  const data = requireObject(raw, what);
  const source = isObject(data.track) ? data.track : data;
  return {
    id: asStringOrNull(source.id),
    title: asStringOrNull(source.title),
    artist: asStringOrNull(source.artist),
    album: asStringOrNull(source.album),
    duration: asNumber(source.duration),
    size: asNumber(source.size),
    bitrate: asNumber(source.bitrate),
    coverUrl: asStringOrNull(source.cover_url ?? source.coverUrl),
  };
}

function normalizePendingMatches(raw: unknown, fallback: DuplicateKind | null): PendingMatch[] {
  if (raw === undefined || raw === null) return [];
  const list = requireArray(raw, 'совпадения файла');
  return list.map((entry) => {
    const data = requireObject(entry, 'совпадение файла');
    const kind =
      normalizeDuplicateKind(data.duplicate_kind) ??
      normalizeDuplicateKind(data.reason) ??
      normalizeDuplicateKind(data.kind) ??
      fallback;
    if (!kind) throw contractError('причина совпадения');
    return { kind, track: normalizeComparison(data, 'трек в совпадении') };
  });
}

function normalizeDecision(value: unknown): DecisionAction | null {
  return value === 'save' || value === 'skip' ? value : null;
}

function normalizePendingUpload(raw: unknown): PendingUpload {
  const data = requireObject(raw, 'ожидающий решения файл');
  const id = requireString(data.id, 'id ожидающего файла');
  const reason = normalizeDuplicateKind(data.reason);
  const metadataRaw = data.metadata;
  const metadata =
    metadataRaw === undefined || metadataRaw === null
      ? null
      : normalizeComparison(metadataRaw, 'метаданные нового файла');
  return {
    id,
    reason,
    metadata,
    matches: normalizePendingMatches(data.matches, reason),
    decision: normalizeDecision(data.decision),
  };
}

const ITEM_STATE_ALIASES: Record<string, UploadItemState> = {
  queued: 'queued',
  waiting: 'queued',
  uploading: 'uploading',
  processing: 'processing',
  added: 'added',
  pending: 'pending',
  pending_decision: 'pending',
  needs_decision: 'pending',
  saved: 'saved',
  duplicate: 'saved',
  skipped: 'skipped',
  cancelled: 'cancelled',
  canceled: 'cancelled',
  error: 'error',
  failed: 'error',
};

function normalizeItemState(value: unknown): UploadItemState {
  const state = ITEM_STATE_ALIASES[asString(value)];
  if (!state) throw contractError('состояние файла загрузки');
  return state;
}

function normalizeBatchStatus(value: unknown): UploadBatchStatus {
  const status = asString(value);
  switch (status) {
    case 'uploading':
    case 'processing':
    case 'pending':
    case 'done':
    case 'cancelled':
    case 'error':
      return status;
    case 'queued':
    case 'active':
      return 'uploading';
    case 'completed':
    case 'finished':
      return 'done';
    case 'canceled':
      return 'cancelled';
    case 'failed':
      return 'error';
    default:
      throw contractError('состояние пакета загрузки');
  }
}

function normalizeBatchCounts(raw: unknown): UploadBatchCounts {
  const data = requireObject(raw, 'счётчики загрузки');
  return {
    added: requireNumber(data.added, 'added загрузки'),
    pending: requireNumber(data.pending, 'pending загрузки'),
    skipped: requireNumber(data.skipped, 'skipped загрузки'),
    errors: requireNumber(data.errors, 'errors загрузки'),
  };
}

function normalizeBatchItem(raw: unknown, fallback: DuplicateKind | null): UploadBatchItem {
  const data = requireObject(raw, 'файл пакета загрузки');
  const id = requireString(data.id, 'id файла загрузки');
  const state = normalizeItemState(data.state);
  const reason = normalizeDuplicateKind(data.reason) ?? fallback;
  const metadataRaw = data.metadata;
  return {
    id,
    filename: requireString(data.filename, 'имя файла загрузки'),
    size: asNumber(data.size),
    state,
    reason,
    metadata:
      metadataRaw === undefined || metadataRaw === null
        ? null
        : normalizeComparison(metadataRaw, 'метаданные файла загрузки'),
    matches: normalizePendingMatches(data.matches, reason),
    decision: normalizeDecision(data.decision),
    error: asStringOrNull(data.error),
  };
}

export function normalizeUploadBatch(raw: unknown): UploadBatch {
  const data = requireObject(raw, 'состояние загрузки');
  const batch = isObject(data.batch) ? data.batch : data;
  const total = requireNumber(batch.total, 'total загрузки');
  const itemsRaw = requireArray(batch.items, 'items загрузки');
  return {
    id: requireString(batch.id, 'id пакета загрузки'),
    status: normalizeBatchStatus(batch.status),
    total,
    counts: normalizeBatchCounts(batch.counts),
    items: itemsRaw.map((item) => normalizeBatchItem(item, null)),
  };
}

export function normalizeUploadResponse(raw: unknown): UploadResponse {
  const data = requireObject(raw, 'результат загрузки файла');
  const status = asString(data.status);
  const batchId = requireString(data.batch_id, 'batch_id');
  const meta = {
    application: 'application' in data ? normalizeApplication(data.application) : null,
    inCurrentCycle: asBoolean(data.in_current_cycle),
    engineSynced: 'engine_synced' in data ? asBoolean(data.engine_synced) : null,
    stateRevision: asNumber(data.state_revision),
    appliedRevision: asNumber(data.applied_revision),
  };
  if (status === 'added') {
    const track = normalizeTrack(data.track);
    if (!track) throw contractError('добавленный трек');
    return { status: 'added', track, batchId, ...meta };
  }
  if (status === 'pending') {
    return {
      status: 'pending',
      pending: normalizePendingUpload(data.pending),
      batchId,
      ...meta,
    };
  }
  throw contractError('статус загрузки файла');
}

/* ------------------------------------------------------------------ */
/* Экспорт (VD-RADIO-MEDIA-02)                                         */
/* ------------------------------------------------------------------ */

const EXPORT_STATES: readonly ExportJobState[] = [
  'queued',
  'preparing',
  'ready',
  'downloading',
  'cancelled',
  'error',
];

export function normalizeExportJob(raw: unknown): ExportJob {
  const data = requireObject(raw, 'задание экспорта');
  const job = isObject(data.job) ? data.job : data;
  const state = asString(job.state) as ExportJobState;
  if (!EXPORT_STATES.includes(state)) throw contractError('состояние экспорта');
  const processed = requireNumber(job.processed, 'processed экспорта');
  const total = requireNumber(job.total, 'total экспорта');
  const archiveRaw = job.archive;
  let archive: ExportJob['archive'] = null;
  if (archiveRaw !== undefined && archiveRaw !== null) {
    const archiveData = requireObject(archiveRaw, 'архив экспорта');
    archive = {
      name: requireString(archiveData.name, 'имя архива'),
      size: requireNumber(archiveData.size, 'размер архива'),
    };
  }
  const progress = asNumber(job.progress);
  // Контракт не фиксирует шкалу progress: принимаем и долю, и проценты.
  const normalizedProgress =
    progress === null ? null : progress > 1 ? progress / 100 : progress;
  return {
    id: requireString(job.id, 'id задания экспорта'),
    state,
    processed,
    total,
    progress:
      normalizedProgress !== null
        ? Math.min(1, Math.max(0, normalizedProgress))
        : total > 0
          ? Math.min(1, Math.max(0, processed / total))
          : 0,
    archive,
    error: asStringOrNull(job.error),
    expiresAt: asStringOrNull(job.expires_at),
  };
}

function normalizeExportFile(raw: unknown): ExportFile {
  const data = requireObject(raw, 'файл экспорта');
  return {
    trackId: requireString(data.track_id, 'track_id файла экспорта'),
    name: requireString(data.name, 'имя файла экспорта'),
    size: requireNumber(data.size, 'размер файла экспорта'),
    url: requireString(data.url, 'url файла экспорта'),
  };
}

export function normalizeExportCreateResult(raw: unknown): ExportCreateResult {
  const data = requireObject(raw, 'создание экспорта');
  if (data.files !== undefined) {
    return {
      kind: 'files',
      files: requireArray(data.files, 'files экспорта').map(normalizeExportFile),
    };
  }
  if (isObject(data.job)) {
    return { kind: 'job', job: normalizeExportJob({ job: data.job }) };
  }
  throw contractError('ответ создания экспорта');
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

function normalizeQueueItem(raw: unknown): QueueItem {
  const data = requireObject(raw, 'элемент очереди');
  const id = asString(data.id) || asString(data.position_id);
  if (!id) throw contractError('id/position_id элемента очереди');
  const trackRaw = data.track;
  const track = normalizeTrack(trackRaw);
  if (!track) throw contractError('трек элемента очереди');
  const source = isObject(data.source) ? data.source : null;
  const sourceText = typeof data.source === 'string' ? data.source : null;
  return {
    id,
    position: asNumber(data.position),
    track,
    sourceKind: source
      ? asStringOrNull(source.kind)
      : sourceText ?? asStringOrNull(data.source_kind),
    sourceLabel: source
      ? asStringOrNull(source.label ?? source.name)
      : asStringOrNull(data.source_label) ?? sourceText,
    prepared: asBoolean(data.prepared),
    manual: asBoolean(data.manual),
    estimatedStart: asStringOrNull(data.estimated_start),
  };
}

export function normalizeQueueSnapshot(raw: unknown): QueueSnapshot {
  const data = requireObject(raw, 'очередь');
  const itemsRaw = requireArray(data.items, 'items очереди');
  const currentRaw = data.current;
  const current =
    currentRaw === null || currentRaw === undefined ? null : normalizeQueueItem(currentRaw);
  const items = itemsRaw.map(normalizeQueueItem);
  return {
    current,
    items,
    depth: asNumber(data.depth),
    stateRevision: asNumber(data.state_revision),
    engineSynced: 'engine_synced' in data ? asBoolean(data.engine_synced) : null,
    appliedRevision: asNumber(data.applied_revision),
    shuffled: asNumber(data.shuffled),
    removed: asNumber(data.removed),
  };
}

const SERVICE_LABELS: Record<string, string> = {
  icecast: 'Icecast',
  liquidsoap: 'Liquidsoap',
  api: 'Backend',
};

const SERVICE_STATE_LABELS: Record<ServiceInfo['state'], string> = {
  active: 'Работает',
  inactive: 'Остановлена',
  failed: 'Ошибка',
  unknown: 'Неизвестно',
};

function serviceIdFromUnit(unit: string | null): string | null {
  if (!unit) return null;
  if (unit.includes('icecast')) return 'icecast';
  if (unit.includes('liquidsoap')) return 'liquidsoap';
  if (unit.includes('api')) return 'api';
  return null;
}

function normalizeServiceState(value: unknown): ServiceInfo['state'] {
  switch (value) {
    case 'active':
    case 'running':
    case 'on':
      return 'active';
    case 'inactive':
    case 'stopped':
    case 'off':
    case 'false':
      return 'inactive';
    case 'failed':
    case 'error':
      return 'failed';
    default:
      return 'unknown';
  }
}

function normalizeService(
  raw: unknown,
  fallbackStart: string | null,
  fallbackError: string | null,
): ServiceInfo {
  const data = requireObject(raw, 'служба');
  const unit = asStringOrNull(data.unit);
  const id = asString(data.id) || serviceIdFromUnit(unit);
  if (!id) throw contractError('id/unit службы');
  const state = normalizeServiceState(data.state);
  const label = asString(data.label ?? data.name) || SERVICE_LABELS[id] || id;
  const stateLabel =
    asString(data.state_label) ||
    SERVICE_STATE_LABELS[state];
  return {
    id,
    unit,
    label,
    state,
    stateLabel,
    detail: asStringOrNull(data.detail),
    lastStart: asStringOrNull(data.last_start) ?? (id === 'liquidsoap' ? fallbackStart : null),
    lastError: asStringOrNull(data.last_error) ?? (id === 'liquidsoap' ? fallbackError : null),
  };
}

export function normalizeServices(raw: unknown): ServiceInfo[] {
  const data = requireObject(raw, 'службы');
  const list = requireArray(data.services, 'services');
  const fallbackStart = asStringOrNull(data.last_start);
  const fallbackError = asStringOrNull(data.last_error);
  return list.map((item) => normalizeService(item, fallbackStart, fallbackError));
}

export function normalizeStorage(raw: unknown): StorageInfo {
  const data = requireObject(raw, 'хранилище');
  return {
    tracks: requireNumber(data.tracks, 'tracks хранилища'),
    bytes: requireNumber(data.bytes, 'bytes хранилища'),
    coverBytes: asNumber(data.cover_bytes) ?? 0,
    dbBytes: asNumber(data.db_bytes) ?? 0,
    freeBytes: requireNumber(data.free_bytes, 'free_bytes хранилища'),
    totalBytes: requireNumber(data.total_bytes, 'total_bytes хранилища'),
    limitGb: asNumber(data.limit_gb) ?? 0,
  };
}

/* ------------------------------------------------------------------ */
/* Ссылки на трансляцию                                                */
/* ------------------------------------------------------------------ */

const LINK_LABELS: Record<string, string> = {
  mp3: 'Прямой MP3',
  http_mp3: 'HTTP MP3',
  player: 'Веб-плеер',
  m3u: 'M3U-плейлист',
  pls: 'PLS-плейлист',
  metadata: 'Метаданные эфира',
};

function normalizeLink(raw: unknown): RadioLink {
  const data = requireObject(raw, 'ссылка на трансляцию');
  const key = requireString(data.key, 'key ссылки');
  const url = requireString(data.url, 'url ссылки');
  const label = asString(data.label) || LINK_LABELS[key] || key;
  return {
    key,
    label,
    url,
    description: asStringOrNull(data.description),
    primary: 'primary' in data ? asBoolean(data.primary) : key === 'mp3',
  };
}

export function normalizeLinks(raw: unknown): RadioLink[] {
  const data = requireObject(raw, 'ссылки на трансляцию');
  const list = requireArray(data.links, 'links');
  return list.map(normalizeLink);
}

/** Публичная страница не имеет доступа к admin-эндпоинту ссылок: собирает
 * ссылки только из публичного now-playing. */
export function publicLinksFromStation(station: NowStation): RadioLink[] {
  const links: RadioLink[] = [];
  if (station.streamUrl) {
    links.push({
      key: 'mp3',
      label: LINK_LABELS.mp3 ?? 'Прямой MP3',
      url: station.streamUrl,
      description: null,
      primary: true,
    });
  }
  if (station.m3uUrl) {
    links.push({
      key: 'm3u',
      label: LINK_LABELS.m3u ?? 'M3U-плейлист',
      url: station.m3uUrl,
      description: 'Плейлист M3U для VLC и мобильных плееров.',
      primary: false,
    });
  }
  if (station.plsUrl) {
    links.push({
      key: 'pls',
      label: LINK_LABELS.pls ?? 'PLS-плейлист',
      url: station.plsUrl,
      description: 'Плейлист PLS для совместимых плееров.',
      primary: false,
    });
  }
  return links;
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
  const defaultPlaylistId = asString(data.default_playlist_id, '');
  return {
    stationName: asString(data.station_name, 'Vanilla Dream Radio'),
    stationDescription: asString(data.station_description, ''),
    stationGenre: asString(data.station_genre, ''),
    bitrate: Math.trunc(asNumber(data.bitrate) ?? 128),
    timezone: asString(data.timezone, 'UTC'),
    scheduleEnabled: asBoolean(data.schedule_enabled, true),
    defaultTransition: asString(data.default_transition, 'after_track'),
    // '' — системный источник «Вся медиатека (автоматически)».
    defaultPlaylistId: defaultPlaylistId === SYSTEM_PLAYLIST_ID ? '' : defaultPlaylistId,
    queueDepth: Math.trunc(asNumber(data.queue_depth) ?? 6),
    maxUploadMb: Math.trunc(asNumber(data.max_upload_mb) ?? 300),
    maxLibraryGb: Math.trunc(asNumber(data.max_library_gb) ?? 0),
    desiredState: desired,
    playbackMode: normalizePlaybackMode(data.playback_mode),
    selectedPlaylistId: asStringOrNull(data.selected_playlist_id),
    engineLastStart: asString(data.engine_last_start, ''),
    engineLastError: asString(data.engine_last_error, ''),
  };
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
  const data = requireObject(raw, 'список треков');
  const items = requireArray(data.items, 'items треков').map((item) => {
    const track = normalizeTrack(item);
    if (!track) throw contractError('трек в медиатеке');
    return track;
  });
  const libraryObj = requireObject(data.library, 'library в списке треков');
  return {
    items,
    total: requireNumber(data.total, 'total треков'),
    library: {
      count: requireNumber(libraryObj.count, 'library.count'),
      bytes: requireNumber(libraryObj.bytes, 'library.bytes'),
    },
  };
}

async function tracksIds(query: { q?: string; excludePlaylistId?: string } = {}): Promise<TrackIdPage> {
  const raw = await request<unknown>('/api/admin/tracks/ids', {
    query: { q: query.q, exclude_playlist_id: query.excludePlaylistId },
  });
  return normalizeTrackIds(raw);
}

function normalizeTrackResponse(raw: unknown, what: string): Track {
  const data = isObject(raw) ? raw : {};
  const track = normalizeTrack(data.track ?? raw);
  if (!track) throw contractError(what);
  return track;
}

interface TrackEditorInput {
  title: string;
  artist: string | null;
  album: string | null;
  coverAction: 'keep' | 'replace' | 'revert';
  cover?: File | null;
}

async function trackUpdate(id: string, payload: TrackEditorInput): Promise<Track> {
  const form = new FormData();
  form.append('title', payload.title);
  form.append('artist', payload.artist ?? '');
  form.append('album', payload.album ?? '');
  form.append('cover_action', payload.coverAction);
  if (payload.cover) form.append('cover', payload.cover, payload.cover.name);
  const raw = await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}`, {
    method: 'PUT',
    body: form,
  });
  return normalizeTrackResponse(raw, 'обновлённый трек');
}

async function trackUploadCover(id: string, file: File): Promise<Track> {
  const form = new FormData();
  form.append('file', file, file.name);
  const raw = await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}/cover`, {
    method: 'POST',
    body: form,
  });
  return normalizeTrackResponse(raw, 'обложка трека');
}

async function trackResetCover(id: string): Promise<Track> {
  const raw = await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}/cover`, {
    method: 'DELETE',
  });
  return normalizeTrackResponse(raw, 'исходная обложка трека');
}

export function trackDownloadUrl(id: string, mode: 'tags' | 'original'): string {
  return `/api/admin/tracks/${encodeURIComponent(id)}/download?mode=${mode}`;
}

/* ------------------------------------------------------------------ */
/* Пакеты загрузки                                                     */
/* ------------------------------------------------------------------ */

async function uploadBatchGet(batchId: string): Promise<UploadBatch> {
  const raw = await request<unknown>(`/api/admin/uploads/${encodeURIComponent(batchId)}`);
  return normalizeUploadBatch(raw);
}

async function uploadDecide(pendingId: string, action: DecisionAction): Promise<void> {
  await request<unknown>(`/api/admin/uploads/${encodeURIComponent(pendingId)}/decide`, {
    method: 'POST',
    body: { action },
  });
}

async function uploadDecideBatch(
  batchId: string,
  action: DecisionAction,
  scope: 'all' | 'search' = 'all',
  query = '',
): Promise<void> {
  await request<unknown>(`/api/admin/uploads/batch/${encodeURIComponent(batchId)}/decide`, {
    method: 'POST',
    body: { action, scope, query },
  });
}

async function uploadCancelBatch(batchId: string): Promise<void> {
  await request<unknown>(`/api/admin/uploads/batch/${encodeURIComponent(batchId)}/cancel`, {
    method: 'POST',
  });
}

/* ------------------------------------------------------------------ */
/* Экспорт                                                             */
/* ------------------------------------------------------------------ */

async function exportsCreate(payload: ExportRequest): Promise<ExportCreateResult> {
  const raw = await request<unknown>('/api/admin/exports', { method: 'POST', body: payload });
  return normalizeExportCreateResult(raw);
}

async function exportGet(jobId: string): Promise<ExportJob> {
  const raw = await request<unknown>(`/api/admin/exports/${encodeURIComponent(jobId)}`);
  return normalizeExportJob(raw);
}

async function exportCancel(jobId: string): Promise<void> {
  await request<unknown>(`/api/admin/exports/${encodeURIComponent(jobId)}/cancel`, {
    method: 'POST',
  });
}

export function exportArchiveUrl(jobId: string): string {
  return `/api/admin/exports/${encodeURIComponent(jobId)}/archive`;
}

async function trackDelete(id: string, mode: DeleteMode = 'after'): Promise<DeleteTrackResult> {
  const raw = await request<unknown>(`/api/admin/tracks/${encodeURIComponent(id)}`, {
    method: 'DELETE',
    body: { mode },
  });
  const data = isObject(raw) ? raw : {};
  return {
    deleted: asBoolean(data.deleted, true),
    playlistsCount: asNumber(data.playlists_count),
    pending: asBoolean(data.pending),
    mode: data.mode === 'now' ? 'now' : data.mode === 'after' ? 'after' : null,
    current: asBoolean(data.current),
    skipped: asBoolean(data.skipped),
  };
}

function stringList(value: unknown, what: string): string[] {
  return asArray(value).map((item) => requireString(item, what));
}

async function tracksBulkDelete(ids: string[], mode: DeleteMode = 'after'): Promise<BulkDeleteResult> {
  const raw = await request<unknown>('/api/admin/tracks/bulk-delete', {
    method: 'POST',
    body: { ids, mode },
  });
  const data = requireObject(raw, 'результат массового удаления');
  return {
    marked: stringList(data.marked, 'marked'),
    deleted: stringList(data.deleted, 'deleted'),
    purged: stringList(data.purged, 'purged'),
    pending: stringList(data.pending, 'pending'),
    notFound: stringList(data.not_found, 'not_found'),
    current: stringList(data.current, 'current'),
    mode: data.mode === 'now' ? 'now' : 'after',
    skipped: asBoolean(data.skipped),
  };
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
  const data = requireObject(raw, 'список плейлистов');
  const items = requireArray(data.items, 'items плейлистов').map(normalizePlaylist);
  return items.sort((a, b) => {
    if (a.system !== b.system) return a.system ? -1 : 1;
    return a.name.localeCompare(b.name, 'ru');
  });
}

async function playlistGet(id: string): Promise<PlaylistDetail> {
  const raw = await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}`);
  return normalizePlaylistDetail(raw);
}

async function playlistCreate(payload: { name: string; description?: string }): Promise<Playlist> {
  const raw = await request<unknown>('/api/admin/playlists', { method: 'POST', body: payload });
  const data = requireObject(raw, 'создание плейлиста');
  const playlist = normalizePlaylist(data.playlist ?? data);
  return playlist;
}

async function playlistUpdate(id: string, patch: Record<string, unknown>): Promise<PlaylistUpdateResult> {
  const raw = await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}`, {
    method: 'PATCH',
    body: patch,
  });
  const data = requireObject(raw, 'обновление плейлиста');
  return {
    playlist: normalizePlaylist(data.playlist ?? data),
    application: normalizeApplication(data.application),
    engineSynced: asBoolean(data.engine_synced),
    stateRevision: asNumber(data.state_revision),
    appliedRevision: asNumber(data.applied_revision),
  };
}

async function playlistDelete(id: string): Promise<void> {
  await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}`, { method: 'DELETE' });
}

async function playlistAddTracks(id: string, trackIds: string[]): Promise<AddTracksResult> {
  const raw = await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/tracks`, {
    method: 'POST',
    body: { track_ids: trackIds },
  });
  return normalizeAddTracksResult(raw);
}

async function playlistRemoveTrack(id: string, trackId: string): Promise<RemoveTrackResult> {
  const raw = await request<unknown>(
    `/api/admin/playlists/${encodeURIComponent(id)}/tracks/${encodeURIComponent(trackId)}`,
    { method: 'DELETE' },
  );
  return normalizeRemoveTrackResult(raw);
}

async function playlistReorder(id: string, trackIds: string[]): Promise<void> {
  await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/order`, {
    method: 'PUT',
    body: { track_ids: trackIds },
  });
}

async function playlistPlay(
  id: string,
  mode: 'now' | 'next' = 'now',
  restart = false,
): Promise<PlaylistPlayResult> {
  const raw = await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/play`, {
    method: 'POST',
    body: { mode, restart },
  });
  const data = requireObject(raw, 'результат запуска плейлиста');
  const statusRaw = asString(data.status);
  const status =
    statusRaw === 'already_active' || statusRaw === 'applied' || statusRaw === 'switching'
      ? statusRaw
      : 'switching';
  const playlist = data.playlist ? normalizePlaylist(data.playlist) : null;
  if (!playlist) throw contractError('плейлист в результате запуска');
  return {
    status,
    started: asBoolean(data.started),
    engineSynced: asBoolean(data.engine_synced),
    playlist,
    station: data.station ? normalizeStationState(data.station) : null,
    stateRevision: asNumber(data.state_revision),
    appliedRevision: asNumber(data.applied_revision),
  };
}

async function playlistRestart(id: string): Promise<PlaylistPlayResult> {
  const raw = await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/restart`, {
    method: 'POST',
  });
  const data = requireObject(raw, 'результат перезапуска плейлиста');
  const playlist = data.playlist ? normalizePlaylist(data.playlist) : null;
  if (!playlist) throw contractError('плейлист в результате перезапуска');
  return {
    status: 'switching',
    started: asBoolean(data.started),
    engineSynced: asBoolean(data.engine_synced),
    playlist,
    station: data.station ? normalizeStationState(data.station) : null,
    stateRevision: asNumber(data.state_revision),
    appliedRevision: asNumber(data.applied_revision),
  };
}

async function playlistUploadCover(id: string, file: File): Promise<Playlist> {
  const form = new FormData();
  form.append('file', file, file.name);
  const raw = await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/cover`, {
    method: 'POST',
    body: form,
  });
  const data = requireObject(raw, 'обложка плейлиста');
  return normalizePlaylist(data.playlist ?? data);
}

async function playlistResetCover(id: string): Promise<Playlist> {
  const raw = await request<unknown>(`/api/admin/playlists/${encodeURIComponent(id)}/cover`, {
    method: 'DELETE',
  });
  const data = requireObject(raw, 'сброс обложки плейлиста');
  return normalizePlaylist(data.playlist ?? data);
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
  return normalizeQueueSnapshot(await request<unknown>('/api/admin/queue'));
}

async function queueMove(id: string, moveTo: number): Promise<QueueSnapshot> {
  return normalizeQueueSnapshot(
    await request<unknown>(`/api/admin/queue/${encodeURIComponent(id)}`, {
      method: 'PATCH',
      body: { move_to: moveTo },
    }),
  );
}

async function queueRemove(id: string): Promise<QueueSnapshot> {
  return normalizeQueueSnapshot(
    await request<unknown>(`/api/admin/queue/${encodeURIComponent(id)}`, { method: 'DELETE' }),
  );
}

async function queueMoveNext(id: string): Promise<QueueSnapshot> {
  return normalizeQueueSnapshot(
    await request<unknown>(`/api/admin/queue/${encodeURIComponent(id)}/move-next`, {
      method: 'POST',
    }),
  );
}

async function queueShuffle(): Promise<QueueSnapshot> {
  return normalizeQueueSnapshot(
    await request<unknown>('/api/admin/queue/shuffle', { method: 'POST' }),
  );
}

async function queueClearManual(): Promise<QueueSnapshot> {
  return normalizeQueueSnapshot(
    await request<unknown>('/api/admin/queue/clear-manual', { method: 'POST' }),
  );
}

/* ------------------------------------------------------------------ */
/* Станция                                                             */
/* ------------------------------------------------------------------ */

async function stationAction(action: 'start' | 'stop' | 'restart' | 'skip'): Promise<void> {
  await request<unknown>(`/api/admin/station/${action}`, { method: 'POST' });
}

async function stationStateGet(): Promise<StationState> {
  return normalizeStationState(await request<unknown>('/api/admin/station/state'));
}

async function stationModeSet(
  mode: PlaybackMode,
  playlistId?: string | null,
): Promise<StationModeResult> {
  const raw = await request<unknown>('/api/admin/station/mode', {
    method: 'PUT',
    body: { mode, playlist_id: playlistId ?? undefined },
  });
  const data = requireObject(raw, 'смена режима станции');
  return {
    state: normalizeStationState(data),
    engineSynced: asBoolean(data.engine_synced),
    appliedRevision: asNumber(data.applied_revision),
  };
}

async function setOverride(payload: {
  playlistId: string;
  scope?: 'until_schedule_change' | 'indefinite';
}): Promise<void> {
  await request<unknown>('/api/admin/station/override', {
    method: 'POST',
    body: {
      kind: 'playlist',
      scope: payload.scope ?? 'until_schedule_change',
      playlist_id: payload.playlistId,
    },
  });
}

async function clearOverride(): Promise<void> {
  await request<unknown>('/api/admin/station/override', { method: 'DELETE' });
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
  return normalizeServices(await request<unknown>('/api/admin/services'));
}

async function linksGet(): Promise<RadioLink[]> {
  return normalizeLinks(await request<unknown>('/api/admin/links'));
}

async function adminHistory(limit = 20): Promise<HistoryEntry[]> {
  const raw = await request<unknown>('/api/admin/history', { query: { limit } });
  const data = requireObject(raw, 'история эфира');
  const items = requireArray(data.items, 'items истории');
  return items.map((entry) => {
    const normalized = normalizeHistoryEntry(entry);
    if (!normalized) throw contractError('запись истории');
    return normalized;
  });
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
      ids: tracksIds,
      update: trackUpdate,
      uploadCover: trackUploadCover,
      resetCover: trackResetCover,
      remove: trackDelete,
      bulkDelete: tracksBulkDelete,
      playNow: trackPlayNow,
      playNext: trackPlayNext,
      enqueue: trackEnqueue,
    },
    uploads: {
      get: uploadBatchGet,
      decide: uploadDecide,
      decideBatch: uploadDecideBatch,
      cancel: uploadCancelBatch,
    },
    exports: {
      create: exportsCreate,
      get: exportGet,
      cancel: exportCancel,
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
      restart: playlistRestart,
      uploadCover: playlistUploadCover,
      resetCover: playlistResetCover,
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
      moveNext: queueMoveNext,
      remove: queueRemove,
      shuffle: queueShuffle,
      clearManual: queueClearManual,
    },
    station: {
      start: () => stationAction('start'),
      stop: () => stationAction('stop'),
      restart: () => stationAction('restart'),
      skip: () => stationAction('skip'),
      setOverride,
      clearOverride,
      state: stationStateGet,
      setMode: stationModeSet,
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
