import { describe, expect, it } from 'vitest';
import {
  PayloadError,
  exportArchiveUrl,
  normalizeExportCreateResult,
  normalizeExportJob,
  normalizeUploadBatch,
  normalizeUploadResponse,
  trackDownloadUrl,
} from '../../src/lib/api';
import { duplicateReasonLabel, duplicateTooltip } from '../../src/lib/media';

const comparison = {
  id: 't1',
  title: 'Ночной трамвай',
  artist: 'Vanilla Dream',
  album: 'Ночные волны',
  duration: 238,
  size: 9520000,
  bitrate: 320,
  cover_url: '/mock-cover.svg',
};

const trackPayload = {
  id: 't9',
  title: 'Ночной трамвай',
  artist: 'Новый артист',
  album: 'Копии',
  genre: 'Ambient',
  year: 2026,
  duration: 238,
  bitrate: 320,
  size: 9520000,
  ext: 'mp3',
  cover_url: '/mock-cover.svg',
  cover_kind: 'custom',
  is_duplicate: true,
  duplicate_kind: 'tags',
  duplicate_of: 't1',
  can_revert_cover: true,
  source_filename: 'ночной трамвай.mp3',
  created_at: '2026-11-25T00:00:00+00:00',
};

const pendingPayload = {
  id: 'u1',
  reason: 'tags',
  metadata: {
    title: 'Ночной трамвай',
    artist: 'Новый артист',
    album: 'Копии',
    duration: 238,
    size: 9520000,
    bitrate: 320,
    cover_url: '/mock-cover.svg',
  },
  matches: [{ duplicate_kind: 'tags', track: comparison }],
  decision: null,
};

describe('track payload 1.1.0', () => {
  it('normalizes duplicate flags and cover fields without losing legacy tracks', () => {
    const added = normalizeUploadResponse({
      status: 'added',
      track: trackPayload,
      batch_id: 'b1',
    });
    expect(added.status).toBe('added');
    if (added.status !== 'added') throw new Error('unreachable');
    expect(added.track.isDuplicate).toBe(true);
    expect(added.track.duplicateKind).toBe('tags');
    expect(added.track.duplicateOf).toBe('t1');
    expect(added.track.coverKind).toBe('custom');
    expect(added.track.canRevertCover).toBe(true);
    expect(added.track.sourceFilename).toBe('ночной трамвай.mp3');
    expect(added.batchId).toBe('b1');

    const legacy = normalizeUploadResponse({
      status: 'added',
      track: { id: 't2', title: 'Старый трек', cover_url: null },
      batch_id: 'b1',
    });
    if (legacy.status !== 'added') throw new Error('unreachable');
    expect(legacy.track.isDuplicate).toBe(false);
    expect(legacy.track.coverKind).toBe('none');
  });
});

describe('upload response', () => {
  it('keeps the pending reason, metadata and matches', () => {
    const result = normalizeUploadResponse({
      status: 'pending',
      pending: pendingPayload,
      batch_id: 'b2',
    });
    expect(result.status).toBe('pending');
    if (result.status !== 'pending') throw new Error('unreachable');
    expect(result.pending.reason).toBe('tags');
    expect(result.pending.metadata?.artist).toBe('Новый артист');
    expect(result.pending.matches[0]?.kind).toBe('tags');
    expect(result.pending.matches[0]?.track.title).toBe('Ночной трамвай');
    expect(result.batchId).toBe('b2');
  });

  it('rejects an unknown upload status or missing batch id', () => {
    expect(() => normalizeUploadResponse({ status: 'weird', batch_id: 'b1' })).toThrow(PayloadError);
    expect(() => normalizeUploadResponse({ status: 'added', track: trackPayload })).toThrow(PayloadError);
    expect(() =>
      normalizeUploadResponse({ status: 'added', track: { title: 'без id' }, batch_id: 'b1' }),
    ).toThrow(PayloadError);
  });
});

describe('upload batch', () => {
  const batch = {
    batch: {
      id: 'b3',
      status: 'pending',
      total: 3,
      counts: { added: 1, pending: 1, skipped: 0, errors: 1 },
      items: [
        {
          id: 'u1',
          filename: 'новый.mp3',
          size: 1000,
          state: 'added',
          reason: null,
          metadata: null,
          matches: [],
          decision: null,
          error: null,
        },
        {
          ...pendingPayload,
          filename: 'копия.mp3',
          size: 9520000,
          state: 'pending',
        },
        {
          id: 'u3',
          filename: 'битый.mp3',
          size: 2000,
          state: 'error',
          reason: null,
          metadata: null,
          matches: [],
          decision: null,
          error: 'Файл повреждён',
        },
      ],
    },
  };

  it('normalizes counts, states, decisions and errors', () => {
    const result = normalizeUploadBatch(batch);
    expect(result.id).toBe('b3');
    expect(result.status).toBe('pending');
    expect(result.total).toBe(3);
    expect(result.counts).toEqual({ added: 1, replaced: 0, pending: 1, skipped: 0, errors: 1 });
    expect(result.items.map((item) => item.state)).toEqual(['added', 'pending', 'error']);
    expect(result.items[1]?.reason).toBe('tags');
    expect(result.items[1]?.matches).toHaveLength(1);
    expect(result.items[2]?.error).toBe('Файл повреждён');
  });

  it('accepts server aliases and rejects a malformed state instead of silently emptying the list', () => {
    const aliased = normalizeUploadBatch({
      ...batch,
      batch: {
        ...batch.batch,
        status: 'active',
        items: [{ ...batch.batch.items[0], state: 'pending_decision' }],
      },
    });
    expect(aliased.status).toBe('uploading');
    expect(aliased.items[0]?.state).toBe('pending');

    expect(() =>
      normalizeUploadBatch({
        ...batch,
        batch: { ...batch.batch, items: [{ ...batch.batch.items[0], state: 'teleported' }] },
      }),
    ).toThrow(PayloadError);
    expect(() => normalizeUploadBatch({ batch: { id: 'b' } })).toThrow(PayloadError);
  });

  it('reads MEDIA-03 replace fields and falls back to the match list for older payloads', () => {
    const candidate = {
      id: 't1',
      title: 'Ночной трамвай',
      artist: 'Vanilla Dream',
      album: 'Ночные волны',
      duration: 238,
      size: 9520000,
      bitrate: 320,
      cover_url: '/mock-cover.svg',
      is_duplicate: true,
    };
    const result = normalizeUploadBatch({
      batch: {
        id: 'b4',
        status: 'pending',
        total: 1,
        counts: { added: 0, replaced: 0, pending: 1, skipped: 0, errors: 0 },
        items: [
          {
            ...pendingPayload,
            filename: 'копия.mp3',
            size: 9520000,
            state: 'pending',
            can_replace: true,
            identical: false,
            replace_target: candidate,
            replace_candidates: [candidate, { ...candidate, id: 't3', title: 'Другое' }],
            decision_target: null,
          },
        ],
      },
    });
    const item = result.items[0];
    expect(item?.canReplace).toBe(true);
    expect(item?.identical).toBe(false);
    expect(item?.replaceTarget?.id).toBe('t1');
    expect(item?.replaceCandidates.map((entry) => entry.id)).toEqual(['t1', 't3']);
    expect(item?.replaceCandidates[0]?.isDuplicate).toBe(true);

    const legacy = normalizeUploadBatch({
      batch: {
        id: 'b5',
        status: 'pending',
        total: 1,
        counts: { added: 0, pending: 1, skipped: 0, errors: 0 },
        items: [{ ...pendingPayload, filename: 'старое.mp3', state: 'pending' }],
      },
    });
    expect(legacy.counts.replaced).toBe(0);
    expect(legacy.items[0]?.replaceCandidates.map((entry) => entry.id)).toEqual(['t1']);
    expect(legacy.items[0]?.canReplace).toBe(true);
    expect(legacy.items[0]?.identical).toBe(false);

    const deletedTarget = normalizeUploadBatch({
      batch: {
        id: 'b6',
        status: 'pending',
        total: 1,
        counts: { added: 0, pending: 1, skipped: 0, errors: 0 },
        items: [
          {
            ...pendingPayload,
            filename: 'удалённая цель.mp3',
            state: 'pending',
            can_replace: false,
            replace_candidates: [],
          },
        ],
      },
    });
    expect(deletedTarget.items[0]?.replaceCandidates).toEqual([]);
    expect(deletedTarget.items[0]?.canReplace).toBe(false);
  });
});

describe('export payloads', () => {
  it('normalizes a preparing job and computes real progress', () => {
    const job = normalizeExportJob({
      job: {
        id: 'j1',
        state: 'preparing',
        processed: 84,
        total: 248,
        archive: null,
        error: null,
        expires_at: '2026-11-25T06:00:00+00:00',
      },
    });
    expect(job.state).toBe('preparing');
    expect(job.progress).toBeCloseTo(84 / 248);
    expect(job.archive).toBeNull();

    const percent = normalizeExportJob({
      id: 'j1b',
      state: 'preparing',
      processed: 84,
      total: 248,
      progress: 34,
    });
    expect(percent.progress).toBeCloseTo(0.34);
  });

  it('normalizes a ready archive and rejects malformed states or archive data', () => {
    const job = normalizeExportJob({
      id: 'j2',
      state: 'ready',
      processed: 10,
      total: 10,
      archive: { name: 'Vanilla Dream Radio.zip', size: 1234 },
      error: null,
    });
    expect(job.progress).toBe(1);
    expect(job.archive).toEqual({ name: 'Vanilla Dream Radio.zip', size: 1234 });

    expect(() => normalizeExportJob({ id: 'j', state: 'teleported', processed: 0, total: 1 })).toThrow(
      PayloadError,
    );
    expect(() =>
      normalizeExportJob({ id: 'j', state: 'ready', processed: 0, total: 1, archive: { name: 'x' } }),
    ).toThrow(PayloadError);
    expect(() => normalizeExportJob({ id: 'j', state: 'ready' })).toThrow(PayloadError);
  });

  it('normalizes the separate-files response', () => {
    const result = normalizeExportCreateResult({
      files: [
        {
          track_id: 't1',
          name: 'Vanilla Dream - Ночной трамвай.mp3',
          size: 9520000,
          url: '/api/admin/tracks/t1/download?mode=tags',
        },
      ],
    });
    expect(result.kind).toBe('files');
    if (result.kind !== 'files') throw new Error('unreachable');
    expect(result.files[0]?.name).toContain('Ночной трамвай');
    expect(result.files[0]?.url).toContain('mode=tags');

    expect(() => normalizeExportCreateResult({ files: [{ track_id: 't1' }] })).toThrow(PayloadError);
    expect(() => normalizeExportCreateResult({ nothing: true })).toThrow(PayloadError);
  });

  it('builds encoded download URLs, not blobs', () => {
    expect(trackDownloadUrl('t 1/2', 'original')).toBe(
      '/api/admin/tracks/t%201%2F2/download?mode=original',
    );
    expect(exportArchiveUrl('j 1')).toBe('/api/admin/exports/j%201/archive');
  });
});

describe('duplicate labels', () => {
  it('uses the contract wording in the badge tooltip', () => {
    expect(duplicateReasonLabel('content')).toBe('Идентичный файл');
    expect(duplicateReasonLabel('tags')).toBe('Совпадают теги');
    expect(duplicateReasonLabel('possible')).toBe('Возможный дубликат');
    expect(duplicateTooltip('tags')).toContain('Загружен как отдельная копия существующего трека');
    expect(duplicateTooltip('tags')).toContain('Совпадают теги');
  });
});
