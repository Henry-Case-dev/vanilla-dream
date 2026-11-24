export type StationStatus =
  | 'off'
  | 'starting'
  | 'on_air'
  | 'no_music'
  | 'reconnecting'
  | 'error'
  | 'unknown';

export type DesiredState = 'playing' | 'stopped';

export type RepeatMode = 'none' | 'all' | 'one';

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
  desired: DesiredState;
  streamUrl: string;
  m3uUrl: string;
  plsUrl: string;
  baseUrl: string;
  bitrate: number | null;
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
}

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
  coverUrl: string | null;
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

export interface Playlist {
  id: string;
  name: string;
  description: string | null;
  coverUrl: string | null;
  trackCount: number | null;
  duration: number | null;
  shuffle: boolean;
  repeatMode: RepeatMode;
  createdAt: string | null;
  updatedAt: string | null;
}

export interface PlaylistDetail {
  playlist: Playlist;
  tracks: Track[];
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
  engineLastStart: string;
  engineLastError: string;
}

export interface StorageInfo {
  tracks: number;
  libraryBytes: number;
  freeBytes: number | null;
  totalBytes: number | null;
}

export interface ServiceInfo {
  id: string;
  name: string;
  state: 'active' | 'inactive' | 'failed' | 'unknown';
  lastStart: string | null;
  lastError: string | null;
}

export interface RadioLink {
  id: string;
  label: string;
  url: string;
  hint: string | null;
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
