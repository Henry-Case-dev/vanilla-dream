import { describe, expect, it } from 'vitest';
import {
  PayloadError,
  normalizeAddTracksResult,
  normalizeQueueSnapshot,
  normalizeStationState,
  normalizeUploadResponse,
} from '../../src/lib/api';

const playlist = {
  id: 'p1',
  name: 'Ночной сиреневый',
  description: 'Ночью',
  system: false,
  shuffle: true,
  repeat_mode: 'one',
  repeat_governed_by: 'playlist',
  cover_url: null,
  cover_kind: 'none',
  track_count: 4,
  duration: 900,
  created_at: '2026-11-20T00:00:00+00:00',
  updated_at: '2026-11-20T00:00:00+00:00',
  pending_delete_count: 0,
};

describe('CORE-03 station state', () => {
  it('reads the exact mode, program, override and revisions block', () => {
    const state = normalizeStationState({
      mode: 'playlist',
      selected_playlist_id: 'p1',
      program_playlist_id: 'p1',
      repeat_governed_by: 'playlist',
      effective_source: {
        kind: 'playlist',
        playlist_id: 'p1',
        name: 'Ночной сиреневый',
        reason: 'selected_playlist',
      },
      pending_source: {
        playlist_id: 'p2',
        mode: 'playlist',
        requested_at: '2026-11-26T05:00:00+00:00',
      },
      override: {
        id: 'ov1',
        kind: 'playlist',
        playlist_id: 'p2',
        playlist_name: 'Утренний свет',
        scope: 'until_schedule_change',
        expires_at: '2026-11-26T06:00:00+00:00',
        active: true,
        created_at: '2026-11-26T05:00:00+00:00',
        note: null,
      },
      desired: 'playing',
      status: 'on_air',
      reason: 'playing',
      engine_instance: 'engine-1',
      state_revision: 14,
      applied_revision: 13,
      notice: 'Выбранный плейлист пуст',
      diagnostic_reason: 'playlist_empty',
    });
    expect(state.mode).toBe('playlist');
    expect(state.programPlaylistId).toBe('p1');
    expect(state.repeatGovernedBy).toBe('playlist');
    expect(state.effectiveSource).toEqual({
      kind: 'playlist',
      playlistId: 'p1',
      name: 'Ночной сиреневый',
      reason: 'selected_playlist',
    });
    expect(state.pendingSource?.playlistId).toBe('p2');
    expect(state.override?.playlistName).toBe('Утренний свет');
    expect(state.override?.id).toBe('ov1');
    expect(state.revision).toBe(14);
    expect(state.appliedRevision).toBe(13);
    expect(state.notice).toContain('пуст');
  });

  it('keeps rotation governance and rejects no valid state silently', () => {
    const state = normalizeStationState({
      mode: 'auto',
      repeat_governed_by: 'rotation',
      effective_source: null,
      desired: 'stopped',
      status: 'off',
      state_revision: 3,
    });
    expect(state.repeatGovernedBy).toBe('rotation');
    expect(state.effectiveSource).toBeNull();
    expect(state.override).toBeNull();
    expect(() => normalizeStationState(null)).toThrow(PayloadError);
  });
});

describe('CORE-03 playlist add result', () => {
  it('reads application, in_current_cycle and revisions instead of guessing', () => {
    const result = normalizeAddTracksResult({
      added: 5,
      skipped: 1,
      unavailable: 0,
      playlist,
      application: 'pending',
      in_current_cycle: true,
      engine_synced: false,
      state_revision: 20,
      applied_revision: 19,
    });
    expect(result.playlist.repeatMode).toBe('one');
    expect(result.playlist.repeatGovernedBy).toBe('playlist');
    expect(result.application).toBe('pending');
    expect(result.inCurrentCycle).toBe(true);
    expect(result.engineSynced).toBe(false);
    expect(result.stateRevision).toBe(20);
    expect(result.appliedRevision).toBe(19);
  });
});

describe('CORE-03 queue snapshot', () => {
  it('keeps the snapshot revision and sync metadata', () => {
    const snapshot = normalizeQueueSnapshot({
      current: null,
      items: [
        {
          id: 'pos-1',
          position: 0,
          track: { id: 't1', title: 'Трек' },
          source: { kind: 'playlist', name: 'Ночной сиреневый' },
          manual: false,
          prepared: true,
          estimated_start: '2026-11-26T05:00:00+00:00',
        },
      ],
      depth: 6,
      state_revision: 21,
      engine_synced: true,
      applied_revision: 21,
      shuffled: 3,
      removed: 1,
    });
    expect(snapshot.stateRevision).toBe(21);
    expect(snapshot.engineSynced).toBe(true);
    expect(snapshot.shuffled).toBe(3);
    expect(snapshot.removed).toBe(1);
    expect(snapshot.items[0]?.position).toBe(0);
  });
});

describe('CORE-03 upload result metadata', () => {
  it('reads the same application block from uploads', () => {
    const result = normalizeUploadResponse({
      status: 'added',
      track: { id: 't9', title: 'Новый', cover_url: null },
      batch_id: 'b1',
      application: 'applied',
      in_current_cycle: true,
      engine_synced: true,
      state_revision: 22,
      applied_revision: 22,
    });
    expect(result.status).toBe('added');
    if (result.status !== 'added') throw new Error('unreachable');
    expect(result.application).toBe('applied');
    expect(result.inCurrentCycle).toBe(true);
    expect(result.stateRevision).toBe(22);
  });
});
