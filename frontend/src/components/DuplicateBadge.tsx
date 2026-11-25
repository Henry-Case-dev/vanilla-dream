import { Copy } from 'lucide-react';
import type { DuplicateKind } from '../lib/types';
import { duplicateTooltip } from '../lib/media';

/** Компактная метка «Дубль» рядом с названием трека. Не колонка таблицы. */
export function DuplicateBadge({ kind }: { kind: DuplicateKind | null }) {
  return (
    <span className="dup-badge" title={duplicateTooltip(kind)}>
      <Copy size={10} aria-hidden="true" />
      Дубль
      <span className="visually-hidden"> — {duplicateTooltip(kind).replace(/\n/g, '. ')}</span>
    </span>
  );
}
