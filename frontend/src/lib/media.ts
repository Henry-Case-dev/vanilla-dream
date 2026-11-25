import type { DuplicateKind } from './types';

const REASON_LABELS: Record<DuplicateKind, string> = {
  content: 'Идентичный файл',
  tags: 'Совпадают теги',
  possible: 'Возможный дубликат',
};

export function duplicateReasonLabel(kind: DuplicateKind | null | undefined): string {
  return kind ? REASON_LABELS[kind] : 'Похожий файл';
}

export function duplicateTooltip(kind: DuplicateKind | null | undefined): string {
  return `Загружен как отдельная копия существующего трека\nСовпадение: ${duplicateReasonLabel(kind)}`;
}

export const DUPLICATE_KINDS: readonly DuplicateKind[] = ['content', 'tags', 'possible'];
