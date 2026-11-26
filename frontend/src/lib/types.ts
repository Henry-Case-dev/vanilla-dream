export type StationStatus =
  | 'off'
  | 'starting'
  | 'on_air'
  | 'no_music'
  | 'reconnecting'
  | 'error'
  | 'unknown';

export type DesiredState = 'playing' | 'stopped';

export type PlaybackMode = 'playlist' | 'auto';

export type RepeatMode = 'none' | 'all' | 'one';

export type RepeatGovernedBy = 'playlist' | 'rotation' | null;

export type ApplicationState = 'saved' | 'applied' | 'pending';

export interface EffectiveSource {
  kind: string;
  playlistId: string | null;
  name: string | null;
  reason: string | null;
}

export interface PendingSource {
  playlistId: string;
  mode: PlaybackMode;
  requestedAt: string | null;
}

export interface OverrideInfo {
  id: string;
  kind: string;
  playlistId: string | null;
  playlistName: string | null;
  scope: string;
  expiresAt: string | null;
  active: boolean;
  createdAt: string | null;
  note: string | null;
}

export interface StationState {
  mode: PlaybackMode;
  selectedPlaylistId: string | null;
  /** Плейлист, чьими shuffle/repeat управляет нижняя панель. */
  programPlaylistId: string | null;
  effectiveSource: EffectiveSource | null;
  pendingSource: PendingSource | null;
  override: OverrideInfo | null;
  desired: DesiredState;
  status: StationStatus;
  reason: string | null;
  repeatGovernedBy: RepeatGovernedBy;
  engineInstance: string | null;
  revision: number;
  appliedRevision: number | null;
  notice: string | null;
  diagnosticReason: string | null;
}

export type DeleteMode = 'after' | 'now';

export type RotationMode = 'sequential' | 'by_count' | 'random';

export interface NowTrack {
  id: string;
  title: string;
  artist: string | null;
  album: string | null;
  genre: string | null;
  year: number | null;
  coverUrl: string | null;
  duration: number | null;
  startedAt: string | null;
  elapsed: number | null;
  progress: number | null;
}

export interface NowSource {
  kind: string;
  label: string;
  playlistId: string | null;
  name: string | null;
}

export interface NowStation {
  name: string;
  description: string | null;
  status: StationStatus;
  statusLabel: string | null;
  reason: string | null;
  desired: DesiredState;
  streamUrl: string;
  m3uUrl: string;
  plsUrl: string;
  baseUrl: string;
  bitrate: number | null;
  mode: PlaybackMode;
  programPlaylistId: string | null;
  effectiveSource: EffectiveSource | null;
  pendingSource: PendingSource | null;
  override: OverrideInfo | null;
  repeatGovernedBy: RepeatGovernedBy;
  revision: number;
  notice: string | null;
  diagnosticReason: string | null;
}

export interface NextScheduleChange {
  at: string;
  ruleName: string | null;
}

export interface NowPlaying {
  station: NowStation;
  track: NowTrack | null;
  source: NowSource | null;
  listeners: { available: boolean; count: number | null };
  nextTrack: { id: string; title: string; artist: string | null; duration: number | null } | null;
  nextScheduleChange: NextScheduleChange | null;
  statsAvailable: boolean;
  queueRevision: number | null;
  historyRevision: number | null;
}

export type DuplicateKind = 'content' | 'tags' | 'possible';
export type TrackCoverKind = 'custom' | 'file' | 'none';

export interface Track {
  id: string;
  title: string;
  artist: string | null;
  album: string | null;
  genre: string | null;
  year: number | null;
  duration: number | null;
  bitrate: number | null;
  size: number | null;
  ext: string | null;
  coverUrl: string | null;
  coverKind: TrackCoverKind;
  isDuplicate: boolean;
  duplicateKind: DuplicateKind | null;
  duplicateOf: string | null;
  canRevertCover: boolean;
  originalCoverUrl: string | null;
  sourceFilename: string | null;
  createdAt: string | null;
  playlistsCount: number | null;
}

export interface TrackPage {
  items: Track[];
  total: number;
  library: { count: number; bytes: number };
}

export type TrackSortField = 'title' | 'artist' | 'album' | 'duration' | 'created';

export interface TrackQuery {
  q?: string;
  sort?: TrackSortField;
  order?: 'asc' | 'desc';
  limit?: number;
  offset?: number;
}

export type PlaylistCoverKind = 'custom' | 'auto' | 'none';

export interface Playlist {
  id: string;
  name: string;
  description: string | null;
  system: boolean;
  coverUrl: string | null;
  coverKind: PlaylistCoverKind;
  trackCount: number;
  duration: number;
  shuffle: boolean;
  repeatMode: RepeatMode;
  createdAt: string | null;
  updatedAt: string | null;
  pendingDeleteCount: number | null;
  repeatGovernedBy: RepeatGovernedBy;
}

export interface PlaylistDetail {
  playlist: Playlist;
  tracks: Track[];
  tracksTotal: number;
}

export interface AddTracksResult {
  added: number;
  skipped: number;
  unavailable: number;
  playlist: Playlist;
  application: ApplicationState | null;
  inCurrentCycle: boolean;
  engineSynced: boolean | null;
  stateRevision: number | null;
  appliedRevision: number | null;
}

export interface RemoveTrackResult {
  removed: boolean;
  playlist: Playlist;
}

export interface TrackIdPage {
  ids: string[];
  total: number;
}

export interface ScheduleRule {
  id: string;
  name: string;
  playlistId: string | null;
  useRotation: boolean;
  rotation: number;
  days: number[];
  start: string;
  end: string;
  enabled: boolean;
  priority: number;
  transition: string;
}

export interface ScheduleRuleInput {
  name: string;
  playlist_id: string | null;
  use_rotation: boolean;
  days: number[];
  start: string;
  end: string;
  enabled: boolean;
  priority: number;
  transition: string;
}

export interface ScheduleData {
  rules: ScheduleRule[];
  timezone: string | null;
  enabled: boolean | null;
}

export interface RotationItem {
  id: string | null;
  playlistId: string;
  playlistName: string | null;
  enabled: boolean;
  trackCount: number;
  position: number;
}

export interface Rotation {
  mode: RotationMode;
  items: RotationItem[];
}

export interface QueueItem {
  id: string;
  position: number | null;
  track: Track | null;
  sourceKind: string | null;
  sourceLabel: string | null;
  prepared: boolean;
  manual: boolean;
  estimatedStart: string | null;
}

export interface QueueSnapshot {
  current: QueueItem | null;
  items: QueueItem[];
  depth: number | null;
  stateRevision: number | null;
  engineSynced: boolean | null;
  appliedRevision: number | null;
  shuffled: number | null;
  removed: number | null;
}

export interface Settings {
  stationName: string;
  stationDescription: string;
  stationGenre: string;
  bitrate: number;
  timezone: string;
  scheduleEnabled: boolean;
  defaultTransition: string;
  defaultPlaylistId: string;
  queueDepth: number;
  maxUploadMb: number;
  maxLibraryGb: number;
  desiredState: DesiredState;
  playbackMode: PlaybackMode;
  selectedPlaylistId: string | null;
  engineLastStart: string;
  engineLastError: string;
}

export interface PlaylistPlayResult {
  status: 'switching' | 'applied' | 'already_active';
  started: boolean;
  engineSynced: boolean;
  playlist: Playlist;
  station: StationState | null;
  stateRevision: number | null;
  appliedRevision: number | null;
}

export interface PlaylistUpdateResult {
  playlist: Playlist;
  application: ApplicationState;
  engineSynced: boolean;
  stateRevision: number | null;
  appliedRevision: number | null;
}

export interface StationModeResult {
  state: StationState;
  engineSynced: boolean;
  appliedRevision: number | null;
}

export interface DeleteTrackResult {
  deleted: boolean;
  pending: boolean;
  playlistsCount: number | null;
  mode: DeleteMode | null;
  current: boolean;
  skipped: boolean;
}

export interface BulkDeleteResult {
  marked: string[];
  deleted: string[];
  purged: string[];
  pending: string[];
  notFound: string[];
  current: string[];
  mode: DeleteMode;
  skipped: boolean;
}

export interface StorageInfo {
  tracks: number;
  bytes: number;
  coverBytes: number;
  dbBytes: number;
  freeBytes: number;
  totalBytes: number;
  limitGb: number;
}

export interface ServiceInfo {
  id: string;
  unit: string | null;
  label: string;
  state: 'active' | 'inactive' | 'failed' | 'unknown';
  stateLabel: string;
  detail: string | null;
  lastStart: string | null;
  lastError: string | null;
}

export interface RadioLink {
  key: string;
  label: string;
  url: string;
  description: string | null;
  primary: boolean;
}

export interface HistoryEntry {
  id: string | null;
  trackId: string | null;
  title: string;
  artist: string | null;
  album: string | null;
  startedAt: string | null;
  endedAt: string | null;
  sourceKind: string | null;
  sourceRef: string | null;
}

export interface LoginResult {
  ok: boolean;
  csrfToken: string | null;
}

export interface MeResult {
  authenticated: boolean;
  csrfToken: string | null;
}

export interface ConflictInfo {
  ruleIds: string[];
  message: string;
}

/* ------------------------------------------------------------------ */
/* Загрузка музыки (VD-RADIO-MEDIA-02)                                 */
/* ------------------------------------------------------------------ */

export type DecisionAction = 'save' | 'skip' | 'replace';

/** Поля сравнения «В медиатеке» / «Новый файл». */
export interface TrackComparison {
  id: string | null;
  title: string | null;
  artist: string | null;
  album: string | null;
  duration: number | null;
  size: number | null;
  bitrate: number | null;
  coverUrl: string | null;
}

/** Существующий трек — кандидат на замену файла (MEDIA-03). */
export interface ReplaceCandidate extends Omit<TrackComparison, 'id'> {
  id: string;
  isDuplicate: boolean;
}

/** Разобранный ответ одиночного решения replace. */
export interface ReplaceDecisionResult {
  status: 'replaced' | 'identical' | 'stale' | 'failed';
  replaced: boolean;
  identical: boolean;
  trackId: string | null;
  track: Track | null;
  engineSynced: boolean;
  error: string | null;
  application: ApplicationState | null;
  stateRevision: number | null;
  appliedRevision: number | null;
}

/** Per-item outcome of the batch replace endpoint. */
export interface ReplaceBatchItem {
  id: string;
  status: string;
  trackId: string | null;
  error: string | null;
}

export interface ReplaceBatchResult {
  replaced: number;
  skipped: number;
  failed: number;
  conflicts: number;
  identical: number;
  items: ReplaceBatchItem[];
  application: ApplicationState | null;
  engineSynced: boolean;
  stateRevision: number | null;
  appliedRevision: number | null;
}

export interface PendingMatch {
  kind: DuplicateKind;
  track: TrackComparison;
}

export interface PendingUpload {
  id: string;
  reason: DuplicateKind | null;
  metadata: TrackComparison | null;
  matches: PendingMatch[];
  decision: DecisionAction | null;
}

export type UploadItemState =
  | 'queued'
  | 'uploading'
  | 'processing'
  | 'added'
  | 'pending'
  | 'saved'
  | 'replaced'
  | 'skipped'
  | 'cancelled'
  | 'error';

export interface UploadBatchItem {
  id: string;
  filename: string;
  size: number | null;
  state: UploadItemState;
  reason: DuplicateKind | null;
  metadata: TrackComparison | null;
  matches: PendingMatch[];
  decision: DecisionAction | null;
  decisionTarget: string | null;
  error: string | null;
  canReplace: boolean;
  identical: boolean;
  replaceTarget: ReplaceCandidate | null;
  replaceCandidates: ReplaceCandidate[];
}

export interface UploadBatchCounts {
  added: number;
  replaced: number;
  pending: number;
  skipped: number;
  errors: number;
}

export type UploadBatchStatus =
  | 'uploading'
  | 'processing'
  | 'pending'
  | 'done'
  | 'cancelled'
  | 'error';

export interface UploadBatch {
  id: string;
  status: UploadBatchStatus;
  total: number;
  counts: UploadBatchCounts;
  items: UploadBatchItem[];
}

export type UploadResponse =
  | {
      status: 'added';
      track: Track;
      batchId: string;
      application: ApplicationState | null;
      inCurrentCycle: boolean;
      engineSynced: boolean | null;
      stateRevision: number | null;
      appliedRevision: number | null;
    }
  | {
      status: 'pending';
      pending: PendingUpload;
      batchId: string;
      application: ApplicationState | null;
      inCurrentCycle: boolean;
      engineSynced: boolean | null;
      stateRevision: number | null;
      appliedRevision: number | null;
    };

/* ------------------------------------------------------------------ */
/* Экспорт (VD-RADIO-MEDIA-02)                                          */
/* ------------------------------------------------------------------ */

export type ExportFormat = 'zip' | 'files';
export type ExportMetadataMode = 'tags' | 'original';
export type ExportScopeKind = 'tracks' | 'playlist' | 'library';
export type ExportJobState =
  | 'queued'
  | 'preparing'
  | 'ready'
  | 'downloading'
  | 'cancelled'
  | 'error';

export interface ExportArchive {
  name: string;
  size: number;
}

export interface ExportJob {
  id: string;
  state: ExportJobState;
  processed: number;
  total: number;
  progress: number;
  archive: ExportArchive | null;
  error: string | null;
  expiresAt: string | null;
}

export interface ExportFile {
  trackId: string;
  name: string;
  size: number;
  url: string;
}

export type ExportCreateResult =
  | { kind: 'job'; job: ExportJob }
  | { kind: 'files'; files: ExportFile[] };

export interface ExportRequest {
  scope: ExportScopeKind;
  ids?: string[];
  playlist_id?: string;
  format: ExportFormat;
  metadata: ExportMetadataMode;
}

/** Состав, который показывает диалог экспорта до создания задания. */
export interface ExportComposition {
  scope: ExportScopeKind;
  playlistId?: string;
  ids?: string[];
  title: string;
  coverUrl: string | null;
  count: number;
  size: number | null;
  /** Размер посчитан по загруженной части состава. */
  partialSize?: boolean;
}
