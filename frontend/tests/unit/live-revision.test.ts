import { describe, expect, it } from 'vitest';
import { isStaleHttp } from '../../src/lib/live';

describe('live revision guard', () => {
  it('ignores an HTTP payload older than the SSE revision', () => {
    expect(isStaleHttp(12, 11)).toBe(true);
    expect(isStaleHttp(12, 12)).toBe(false);
    expect(isStaleHttp(12, 13)).toBe(false);
  });
});
