import { describe, expect, it } from 'vitest';
import { formatTime, pluralRu } from '../../src/lib/format';

describe('formatTime applies the requested timezone', () => {
  it('renders winter time in Amsterdam with the standard offset', () => {
    expect(formatTime('2026-01-15T09:40:00Z', 'Europe/Amsterdam')).toBe('10:40');
  });

  it('renders summer time in Amsterdam with daylight saving', () => {
    expect(formatTime('2026-07-15T09:40:00Z', 'Europe/Amsterdam')).toBe('11:40');
  });

  it('renders a near-midnight instant across zones, including the next day', () => {
    const value = '2026-11-26T23:30:00Z';
    expect(formatTime(value, 'UTC')).toBe('23:30');
    expect(formatTime(value, 'Europe/Moscow')).toBe('02:30');
    expect(formatTime(value, 'Asia/Kamchatka')).toBe('11:30');
  });

  it('returns an em dash for a missing timestamp', () => {
    expect(formatTime(null, 'UTC')).toBe('—');
  });
});

describe('pluralRu listener forms', () => {
  const listener = (count: number) =>
    `${count} ${pluralRu(count, ['слушатель', 'слушателя', 'слушателей'])}`;

  it('uses the documented Russian forms', () => {
    expect(listener(1)).toBe('1 слушатель');
    expect(listener(2)).toBe('2 слушателя');
    expect(listener(5)).toBe('5 слушателей');
    expect(listener(11)).toBe('11 слушателей');
    expect(listener(21)).toBe('21 слушатель');
  });
});
