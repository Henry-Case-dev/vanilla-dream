export type ScopeSelectionState = 'none' | 'some' | 'all';

/** Состояние массового выбора в пределах текущей выборки. */
export function scopeSelectionState(
  selected: ReadonlySet<string>,
  scopeIds: readonly string[],
): ScopeSelectionState {
  if (scopeIds.length === 0) return 'none';
  let count = 0;
  for (const id of scopeIds) {
    if (selected.has(id)) count += 1;
  }
  if (count === 0) return 'none';
  return count === scopeIds.length ? 'all' : 'some';
}

/** Tri-state «Выбрать все»: добавляет или снимает только текущую выборку,
 * не затрагивая выбранное в других поисковых запросах. */
export function toggleScopeSelection(
  selected: ReadonlySet<string>,
  scopeIds: readonly string[],
): Set<string> {
  const next = new Set(selected);
  const state = scopeSelectionState(selected, scopeIds);
  if (state === 'all') {
    for (const id of scopeIds) next.delete(id);
  } else {
    for (const id of scopeIds) next.add(id);
  }
  return next;
}

/** Сколько выбранных треков действительно новые для плейлиста. */
export function countNewSelection(
  selected: ReadonlySet<string>,
  existingIds: ReadonlySet<string>,
): number {
  let count = 0;
  for (const id of selected) {
    if (!existingIds.has(id)) count += 1;
  }
  return count;
}

/** Убирает из выбора треки, уже присутствующие в плейлисте. */
export function excludeExisting(
  selected: ReadonlySet<string>,
  existingIds: ReadonlySet<string>,
): Set<string> {
  const next = new Set<string>();
  for (const id of selected) {
    if (!existingIds.has(id)) next.add(id);
  }
  return next;
}
