import { useEffect, useMemo, useRef, useState } from 'react';
import {
  ArrowLeft,
  Check,
  ChevronDown,
  Copy,
  ListChecks,
  RefreshCw,
  Search,
  SkipForward,
} from 'lucide-react';
import { Button, Chip, Spinner, TextField } from '../components/ui';
import { Modal } from '../components/Modal';
import { EmptyState, InlineError } from '../components/States';
import { Cover } from '../components/Cover';
import { useToast } from '../components/Toasts';
import { ApiError, api, errorMessage } from '../lib/api';
import { formatBytes, formatDuration } from '../lib/format';
import { duplicateReasonLabel } from '../lib/media';
import { notifyDataChanged } from '../lib/refresh';
import { useNowPlaying } from '../lib/live';
import type {
  DecisionAction,
  ReplaceCandidate,
  TrackComparison,
  UploadBatchItem,
} from '../lib/types';

interface DecisionsDialogProps {
  open: boolean;
  items: UploadBatchItem[];
  uploading: boolean;
  initialPendingId?: string | null;
  onClose: () => void;
  onBack: () => void;
  onChanged: () => Promise<void>;
}

type OutcomeStatus = 'failed' | 'stale' | 'conflict' | 'identical';

interface ItemOutcome {
  status: OutcomeStatus;
  message: string;
}

interface ApplySummary {
  added: number;
  replaced: number;
  skipped: number;
  identical: number;
  errors: Array<{ id: string; filename: string; message: string }>;
}

const EMPTY_SUMMARY: ApplySummary = { added: 0, replaced: 0, skipped: 0, identical: 0, errors: [] };

function comparisonValue(value: unknown): string {
  if (value === null || value === undefined || value === '') return '—';
  return String(value);
}

function CompareColumn({
  title,
  data,
  other,
}: {
  title: string;
  data: TrackComparison | null;
  other: TrackComparison | null;
}) {
  const rows: Array<{ label: string; value: string; differs: boolean }> = [
    { label: 'Название', key: 'title' },
    { label: 'Исполнитель', key: 'artist' },
    { label: 'Альбом', key: 'album' },
    { label: 'Длительность', key: 'duration' },
    { label: 'Размер', key: 'size' },
    { label: 'Битрейт', key: 'bitrate' },
  ].map(({ label, key }) => {
    const raw = data ? (data as unknown as Record<string, unknown>)[key] : null;
    const otherRaw = other ? (other as unknown as Record<string, unknown>)[key] : null;
    let formatted = comparisonValue(raw);
    if (key === 'duration' && typeof raw === 'number') formatted = formatDuration(raw);
    if (key === 'size' && typeof raw === 'number') formatted = formatBytes(raw);
    if (key === 'bitrate' && typeof raw === 'number') formatted = `${raw} кбит/с`;
    return { label, value: formatted, differs: comparisonValue(raw) !== comparisonValue(otherRaw) };
  });

  return (
    <div className="compare__col">
      <h4 className="compare__title">{title}</h4>
      <Cover src={data?.coverUrl ?? null} alt="" size="lg" rounded="md" className="compare__cover" />
      <dl className="compare__list">
        {rows.map((row) => (
          <div key={row.label} className={`compare__row ${row.differs ? 'is-diff' : ''}`.trim()}>
            <dt>{row.label}</dt>
            <dd>{row.value}</dd>
          </div>
        ))}
      </dl>
    </div>
  );
}

function CandidateRow({
  candidate,
  selected,
  disabled,
  onSelect,
}: {
  candidate: ReplaceCandidate;
  selected: boolean;
  disabled: boolean;
  onSelect: () => void;
}) {
  return (
    <button
      type="button"
      role="radio"
      aria-checked={selected}
      className={`target-option ${selected ? 'is-selected' : ''}`.trim()}
      disabled={disabled}
      onClick={onSelect}
    >
      <span className={`target-option__check ${selected ? 'is-on' : ''}`.trim()} aria-hidden="true">
        {selected && <Check size={12} />}
      </span>
      <Cover src={candidate.coverUrl} alt="" size="xs" />
      <span className="target-option__text">
        <span className="target-option__title">
          <span className="target-option__name">{candidate.title ?? '—'}</span>
          {candidate.isDuplicate && (
            <span className="dup-badge" title="Трек уже отмечен как дубликат">
              <Copy size={10} aria-hidden="true" />
              Дубль
            </span>
          )}
        </span>
        <span className="target-option__sub">
          {candidate.artist ?? '—'} · {candidate.album ?? '—'}
        </span>
        <span className="target-option__meta">
          {formatDuration(candidate.duration)} · {formatBytes(candidate.size)} ·{' '}
          {candidate.bitrate !== null ? `${candidate.bitrate} кбит/с` : 'битрейт неизвестен'}
        </span>
      </span>
    </button>
  );
}

export function DecisionsDialog({
  open,
  items,
  uploading,
  initialPendingId,
  onClose,
  onBack,
  onChanged,
}: DecisionsDialogProps) {
  const toast = useToast();
  const now = useNowPlaying();
  const playingTrackId = now?.track?.id ?? null;
  const [search, setSearch] = useState('');
  const [decisions, setDecisions] = useState<Record<string, DecisionAction>>({});
  const [targets, setTargets] = useState<Record<string, string>>({});
  const [outcomes, setOutcomes] = useState<Record<string, ItemOutcome>>({});
  const [result, setResult] = useState<ApplySummary | null>(null);
  const [bulkNotice, setBulkNotice] = useState<string | null>(null);
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);
  const applying = useRef(false);

  useEffect(() => {
    if (!open) return;
    setSearch('');
    setDecisions({});
    setTargets({});
    setOutcomes({});
    setResult(null);
    setBulkNotice(null);
    setExpanded(new Set(initialPendingId ? [initialPendingId] : []));
    setError(null);
  }, [open, initialPendingId]);

  // Решения, принятые на сервере (например, другим действием), не теряются.
  useEffect(() => {
    setDecisions((current) => {
      let changed = false;
      const next = { ...current };
      items.forEach((item) => {
        if (item.decision && !next[item.id]) {
          next[item.id] = item.decision;
          changed = true;
        }
      });
      return changed ? next : current;
    });
  }, [items]);

  const pendingItems = useMemo(() => items.filter((item) => item.state === 'pending'), [items]);

  const effective = (item: UploadBatchItem): DecisionAction | null =>
    decisions[item.id] ?? item.decision;

  const targetsFor = (item: UploadBatchItem): ReplaceCandidate[] => item.replaceCandidates;

  const targetFor = (item: UploadBatchItem): string | null => {
    const candidates = targetsFor(item);
    const chosen = targets[item.id];
    if (chosen && candidates.some((candidate) => candidate.id === chosen)) return chosen;
    if (candidates.length === 1) return candidates[0].id;
    return null;
  };

  const remaining = pendingItems.filter((item) => !effective(item)).length;
  const replaces = pendingItems.filter((item) => effective(item) === 'replace').length;
  const saves = pendingItems.filter((item) => effective(item) === 'save').length;
  const skips = pendingItems.filter((item) => effective(item) === 'skip').length;
  const missingTargets = pendingItems.filter(
    (item) => effective(item) === 'replace' && !targetFor(item),
  ).length;

  const query = search.trim().toLowerCase();
  const filtered = useMemo(() => {
    if (!query) return pendingItems;
    return pendingItems.filter((item) => {
      const fields = [
        item.filename,
        item.metadata?.title,
        item.metadata?.artist,
        item.metadata?.album,
        ...item.matches.map((match) => match.track.title),
        ...item.matches.map((match) => match.track.artist),
      ];
      return fields.some((field) => (field ?? '').toLowerCase().includes(query));
    });
  }, [pendingItems, query]);

  const clearOutcome = (id: string) => {
    setOutcomes((current) => {
      if (!(id in current)) return current;
      const next = { ...current };
      delete next[id];
      return next;
    });
  };

  const decide = (id: string, action: DecisionAction) => {
    const item = pendingItems.find((entry) => entry.id === id);
    setDecisions((current) => ({ ...current, [id]: action }));
    if (item && action === 'replace') {
      const candidates = targetsFor(item);
      if (candidates.length === 1) {
        const only = candidates[0].id;
        setTargets((current) => (current[id] === only ? current : { ...current, [id]: only }));
      }
    }
    clearOutcome(id);
    setBulkNotice(null);
  };

  const chooseTarget = (id: string, trackId: string) => {
    setTargets((current) => ({ ...current, [id]: trackId }));
    clearOutcome(id);
    setBulkNotice(null);
  };

  const decideRemaining = (action: DecisionAction) => {
    setDecisions((current) => {
      const next = { ...current };
      pendingItems.forEach((item) => {
        if (!effective(item)) next[item.id] = action;
      });
      return next;
    });
    setOutcomes({});
    setBulkNotice(null);
  };

  const replaceRemaining = () => {
    const undecided = pendingItems.filter((item) => !effective(item));
    const nextDecisions = { ...decisions };
    const nextTargets = { ...targets };
    const nextOutcomes: Record<string, ItemOutcome> = {};
    const claimed = new Set(
      pendingItems
        .filter((item) => effective(item) === 'replace')
        .map((item) => targetFor(item))
        .filter((targetId): targetId is string => Boolean(targetId)),
    );
    let selected = 0;
    let needsTarget = 0;
    let noTarget = 0;
    let conflicts = 0;
    undecided.forEach((item) => {
      const candidates = targetsFor(item);
      if (item.identical || !item.canReplace || candidates.length === 0) {
        noTarget += 1;
        return;
      }
      if (candidates.length > 1) {
        needsTarget += 1;
        return;
      }
      const targetId = candidates[0].id;
      if (claimed.has(targetId)) {
        conflicts += 1;
        nextOutcomes[item.id] = {
          status: 'conflict',
          message: 'Несколько файлов претендуют на один трек. Выберите одну версию.',
        };
        return;
      }
      claimed.add(targetId);
      nextDecisions[item.id] = 'replace';
      nextTargets[item.id] = targetId;
      selected += 1;
    });
    if (selected > 0) {
      setDecisions(nextDecisions);
      setTargets(nextTargets);
    }
    if (Object.keys(nextOutcomes).length > 0) {
      setOutcomes((current) => ({ ...current, ...nextOutcomes }));
    }
    const parts = [`Заменено будет: ${selected}`];
    if (needsTarget > 0) parts.push(`нужен выбор цели: ${needsTarget}`);
    if (noTarget > 0) parts.push(`без доступной цели: ${noTarget}`);
    if (conflicts > 0) parts.push(`конфликт цели: ${conflicts}`);
    setBulkNotice(parts.join(' · '));
  };

  const scrollToDecision = (id: string) => {
    window.setTimeout(() => {
      const list = listRef.current;
      if (!list) return;
      const selector = `[data-decision-id="${id.replace(/["\\]/g, '\\$&')}"]`;
      const item = list.querySelector<HTMLElement>(selector);
      if (!item) return;
      // Прокручиваем только список решений, не трогая тело диалога.
      const top =
        item.getBoundingClientRect().top - list.getBoundingClientRect().top + list.scrollTop - 2;
      list.scrollTo({ top: Math.max(0, top), behavior: 'auto' });
    }, 0);
  };

  const toggleExpanded = (id: string) => {
    const opening = !expanded.has(id);
    setExpanded((current) => {
      const next = new Set(current);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
    if (opening) scrollToDecision(id);
  };

  // Открытый по кнопке «Проверить совпадение» файл показываем целиком.
  useEffect(() => {
    if (!open || !initialPendingId || !expanded.has(initialPendingId)) return;
    scrollToDecision(initialPendingId);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [open, initialPendingId, expanded]);

  const mergeSummary = (
    previous: ApplySummary | null,
    delta: ApplySummary,
    touched: string[],
  ): ApplySummary => {
    const base = previous ?? { ...EMPTY_SUMMARY, errors: [] };
    return {
      added: base.added + delta.added,
      replaced: base.replaced + delta.replaced,
      skipped: base.skipped + delta.skipped,
      identical: base.identical + delta.identical,
      errors: [
        ...base.errors.filter((entry) => !touched.includes(entry.id)),
        ...delta.errors,
      ],
    };
  };

  const sendEntries = async (
    entries: Array<{ item: UploadBatchItem; action: DecisionAction; target: string | null }>,
    reset: boolean,
  ) => {
    if (entries.length === 0) return;
    if (applying.current) return;
    applying.current = true;
    if (reset) setResult(null);
    setBusy(true);
    setError(null);
    const delta: ApplySummary = { ...EMPTY_SUMMARY, errors: [] };
    const nextOutcomes: Record<string, ItemOutcome> = {};
    const clearDecisions: string[] = [];
    const claimed = new Map<string, string>();

    for (const entry of entries) {
      const { item, action } = entry;
      const targetId = action === 'replace' ? entry.target : null;
      if (action === 'replace') {
        if (!targetId) {
          const message = 'Выберите трек для замены.';
          nextOutcomes[item.id] = { status: 'failed', message };
          delta.errors.push({ id: item.id, filename: item.filename, message });
          continue;
        }
        const owner = claimed.get(targetId);
        if (owner && owner !== item.id) {
          const message = 'Несколько файлов претендуют на один трек. Выберите одну версию.';
          nextOutcomes[item.id] = { status: 'conflict', message };
          delta.errors.push({ id: item.id, filename: item.filename, message });
          continue;
        }
        claimed.set(targetId, item.id);
      }

      try {
        const response = await api.admin.uploads.decide(item.id, action, targetId);
        if (action === 'replace') {
          if (response?.status === 'replaced' || response?.replaced) {
            delta.replaced += 1;
            clearDecisions.push(item.id);
          } else if (response?.status === 'identical' || response?.identical) {
            delta.identical += 1;
            clearDecisions.push(item.id);
            nextOutcomes[item.id] = {
              status: 'identical',
              message: 'Файлы идентичны — замена не требуется.',
            };
          } else if (response?.status === 'stale') {
            clearDecisions.push(item.id);
            const message =
              response.error ?? 'Совпадение изменилось. Проверьте трек и подтвердите замену.';
            nextOutcomes[item.id] = { status: 'stale', message };
            delta.errors.push({ id: item.id, filename: item.filename, message });
          } else if (response?.status === 'failed') {
            const message = response.error ?? 'Не удалось заменить трек.';
            nextOutcomes[item.id] = { status: 'failed', message };
            delta.errors.push({ id: item.id, filename: item.filename, message });
          }
        } else if (action === 'save') {
          delta.added += 1;
          clearDecisions.push(item.id);
        } else {
          delta.skipped += 1;
          clearDecisions.push(item.id);
        }
      } catch (caught) {
        const code = caught instanceof ApiError ? caught.code : null;
        const message = errorMessage(caught);
        if (code === 'stale_match') {
          clearDecisions.push(item.id);
          nextOutcomes[item.id] = { status: 'stale', message };
        } else {
          nextOutcomes[item.id] = { status: 'failed', message };
        }
        delta.errors.push({ id: item.id, filename: item.filename, message });
      }
    }

    const touched = entries.map((entry) => entry.item.id);
    setOutcomes((current) => {
      const next = { ...current };
      touched.forEach((id) => delete next[id]);
      return { ...next, ...nextOutcomes };
    });
    setResult((current) => mergeSummary(current, delta, touched));
    if (delta.added + delta.replaced + delta.skipped + delta.identical > 0) {
      notifyDataChanged();
    }
    if (clearDecisions.length > 0) {
      setDecisions((current) => {
        const next = { ...current };
        clearDecisions.forEach((id) => delete next[id]);
        return next;
      });
      setTargets((current) => {
        const next = { ...current };
        clearDecisions.forEach((id) => delete next[id]);
        return next;
      });
    }
    try {
      await onChanged();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      applying.current = false;
      setBusy(false);
    }
  };

  const apply = async () => {
    if (remaining > 0 || missingTargets > 0) return;
    const entries = pendingItems
      .map((item) => ({ item, action: effective(item), target: targetFor(item) }))
      .filter(
        (
          entry,
        ): entry is { item: UploadBatchItem; action: DecisionAction; target: string | null } =>
          entry.action !== null,
      );
    if (entries.length === 0) {
      onClose();
      return;
    }
    await sendEntries(entries, true);
    if (entries.every((entry) => entry.action === 'skip')) {
      toast.notify('info', 'Все оставшиеся файлы пропущены');
    }
  };

  const retry = async (item: UploadBatchItem) => {
    const action = effective(item);
    if (!action) return;
    await sendEntries([{ item, action, target: action === 'replace' ? targetFor(item) : null }], false);
  };

  const applyDisabled = remaining > 0 || missingTargets > 0 || pendingItems.length === 0;
  const applyTitle =
    remaining > 0
      ? 'Сначала примите решение по каждому файлу'
      : missingTargets > 0
        ? 'Выберите трек для замены'
        : undefined;

  return (
    <Modal
      open={open}
      onClose={() => (busy ? undefined : onClose())}
      title="Обнаружены совпадения"
      size="lg"
      description={`Файлов ждут решения: ${pendingItems.length}. Ничего не меняется, пока вы не примените решения.`}
      footer={
        <>
          <div className="decisions-footer__counts">
            <span>Осталось: <strong>{remaining}</strong></span>
            <span className="is-accent">Заменить: <strong>{replaces}</strong></span>
            <span className="is-ok">Сохранить: <strong>{saves}</strong></span>
            <span className="is-muted">Пропустить: <strong>{skips}</strong></span>
          </div>
          <div className="decisions-footer__actions">
            <Button variant="ghost" icon={ArrowLeft} onClick={onBack} disabled={busy}>
              Назад к загрузке
            </Button>
            <Button
              variant="primary"
              icon={Check}
              onClick={() => void apply()}
              loading={busy}
              disabled={applyDisabled}
              title={applyTitle}
            >
              Применить решения
            </Button>
          </div>
        </>
      }
    >
      {uploading && (
        <p className="decisions-live">
          <Spinner size={14} /> Загрузка продолжается — новые совпадения появятся здесь автоматически.
        </p>
      )}

      <div className="decisions-bulk">
        <div className="decisions-bulk__note">
          Действие применяется только к нерешённым файлам. Уже принятые решения не меняются.
          Замена требует однозначной цели; при нескольких совпадениях выберите трек вручную.
        </div>
        <div className="decisions-bulk__actions">
          <Button
            variant="secondary"
            size="sm"
            icon={SkipForward}
            disabled={remaining === 0 || busy}
            onClick={() => decideRemaining('skip')}
          >
            Пропустить оставшиеся
          </Button>
          <Button
            variant="secondary"
            size="sm"
            icon={RefreshCw}
            disabled={remaining === 0 || busy}
            onClick={replaceRemaining}
          >
            Заменить оставшиеся
          </Button>
          <Button
            variant="secondary"
            size="sm"
            icon={Check}
            disabled={remaining === 0 || busy}
            onClick={() => decideRemaining('save')}
          >
            Сохранить оставшиеся
          </Button>
        </div>
      </div>
      {bulkNotice && (
        <p className="decisions-bulk__notice" role="status">
          {bulkNotice}
        </p>
      )}
      {missingTargets > 0 && (
        <p className="decisions-bulk__notice" role="status">
          Выберите трек для замены: {missingTargets}
        </p>
      )}

      <div className="search-field dialog-search">
        <Search size={15} aria-hidden="true" />
        <TextField
          label="Поиск совпадений"
          hideLabel
          placeholder="Имя файла, название, исполнитель"
          value={search}
          onChange={(event) => setSearch(event.target.value)}
        />
      </div>

      {error && <InlineError message={error} />}

      {result && (
        <div className="decisions-result" role="status" aria-live="polite">
          <div className="decisions-result__counts">
            <span className="is-ok">Добавлено: <strong>{result.added}</strong></span>
            <span className="is-accent">Заменено: <strong>{result.replaced}</strong></span>
            <span className="is-muted">Пропущено: <strong>{result.skipped}</strong></span>
            {result.identical > 0 && (
              <span>Идентичных: <strong>{result.identical}</strong></span>
            )}
          </div>
          {result.errors.length > 0 && (
            <ul className="decisions-result__errors">
              {result.errors.map((entry) => (
                <li key={entry.id}>
                  <strong>{entry.filename}</strong>: {entry.message}
                </li>
              ))}
            </ul>
          )}
        </div>
      )}

      {pendingItems.length === 0 ? (
        <EmptyState
          compact
          icon={ListChecks}
          title="Совпадений не осталось"
          description={
            result
              ? 'Решения применены. Итог — в сводке выше.'
              : 'Все решения по этой загрузке уже приняты.'
          }
          action={
            <Button variant="secondary" size="sm" onClick={onBack}>
              Назад к загрузке
            </Button>
          }
        />
      ) : filtered.length === 0 ? (
        <EmptyState
          compact
          icon={Search}
          title="Ничего не найдено"
          description={`По запросу «${search.trim()}» совпадений нет.`}
        />
      ) : (
        <ul className="decisions-list" ref={listRef}>
          {filtered.map((item) => {
            const decision = effective(item);
            const outcome = outcomes[item.id];
            const match = item.matches[0] ?? null;
            const otherMatches = item.matches.slice(1);
            const isOpen = expanded.has(item.id);
            const candidates = targetsFor(item);
            const targetId = targetFor(item);
            const chosenCandidate =
              candidates.find((candidate) => candidate.id === targetId) ?? null;
            const replaceBlocked = item.identical
              ? 'Файлы идентичны'
              : !item.canReplace
                ? 'Нет доступного трека для замены'
                : undefined;
            const libraryComparison =
              decision === 'replace' && chosenCandidate ? chosenCandidate : (match?.track ?? null);
            const replaceTitle =
              replaceBlocked ??
              (candidates.length > 1 && decision === 'replace' && !targetId
                ? 'Выберите трек для замены'
                : undefined);
            return (
              <li
                key={item.id}
                data-decision-id={item.id}
                className={`decision ${decision ? `decision--${decision}` : ''}`.trim()}
              >
                <div className="decision__head">
                  <Cover src={item.metadata?.coverUrl ?? null} alt="" size="xs" />
                  <div className="decision__text">
                    <span className="decision__name" title={item.filename}>
                      {item.filename}
                    </span>
                    <span className="decision__sub">
                      {item.metadata?.artist ?? '—'} · {item.metadata?.title ?? '—'} ·{' '}
                      {formatBytes(item.size)}
                    </span>
                  </div>
                  <Chip tone={item.reason === 'possible' ? 'neutral' : 'warning'}>
                    {duplicateReasonLabel(item.reason)}
                  </Chip>
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={ChevronDown}
                    aria-expanded={isOpen}
                    onClick={() => toggleExpanded(item.id)}
                  >
                    {isOpen ? 'Свернуть сравнение' : 'Сравнить файлы'}
                  </Button>
                </div>

                {isOpen && (
                  <div className="decision__compare">
                    <div className="compare">
                      <CompareColumn title="В медиатеке" data={libraryComparison} other={item.metadata} />
                      <CompareColumn title="Новый файл" data={item.metadata} other={libraryComparison} />
                    </div>
                    {otherMatches.length > 0 && (
                      <details className="decision__more">
                        <summary>Ещё совпадения: {otherMatches.length}</summary>
                        <ul>
                          {otherMatches.map((entry, index) => (
                            <li key={`${entry.track.id ?? index}-${index}`}>
                              {entry.track.title ?? '—'} — {entry.track.artist ?? '—'}
                            </li>
                          ))}
                        </ul>
                      </details>
                    )}
                  </div>
                )}

                {item.identical && (
                  <p className="decision__reason">
                    Файлы идентичны — замена не требуется, можно сохранить копию.
                  </p>
                )}
                {!item.identical && !item.canReplace && (
                  <p className="decision__reason">
                    Замена недоступна: нет подходящего трека. Можно сохранить копию или
                    пропустить файл.
                  </p>
                )}

                {decision === 'replace' && (
                  <div className="decision__replace">
                    <p className="decision__note" role="note">
                      Аудиофайл будет заменён. Плейлисты и пользовательские изменения сохранятся.
                      {targetId && playingTrackId === targetId && (
                        <span className="decision__note-playing"> Текущий трек доиграет до конца.</span>
                      )}
                    </p>
                    {chosenCandidate && candidates.length === 1 && (
                      <p className="decision__target-current">
                        Будет заменён: {chosenCandidate.title ?? '—'} —{' '}
                        {chosenCandidate.artist ?? '—'}
                      </p>
                    )}
                    {candidates.length > 1 && (
                      <div
                        className="decision__targets"
                        role="radiogroup"
                        aria-label="Трек для замены"
                      >
                        <span className="decision__targets-label">Выберите трек для замены</span>
                        {candidates.map((candidate) => (
                          <CandidateRow
                            key={candidate.id}
                            candidate={candidate}
                            selected={targetId === candidate.id}
                            disabled={busy}
                            onSelect={() => chooseTarget(item.id, candidate.id)}
                          />
                        ))}
                      </div>
                    )}
                  </div>
                )}

                {outcome && (
                  <div
                    className={`decision__outcome decision__outcome--${outcome.status}`.trim()}
                    role="status"
                  >
                    <span className="decision__outcome-text">
                      {outcome.message}
                      {outcome.status === 'stale' && ' Выберите решение заново.'}
                    </span>
                    {outcome.status === 'failed' && (
                      <Button
                        variant="ghost"
                        size="sm"
                        icon={RefreshCw}
                        disabled={busy}
                        onClick={() => void retry(item)}
                      >
                        Повторить
                      </Button>
                    )}
                  </div>
                )}

                <div className="decision__actions">
                  {item.decision ? (
                    <span className="decision__decided">
                      {item.decision === 'save'
                        ? 'Сохранён как дубликат'
                        : item.decision === 'replace'
                          ? 'Заменён'
                          : 'Пропущен'}
                    </span>
                  ) : (
                    <>
                      <Button
                        variant="secondary"
                        size="sm"
                        icon={SkipForward}
                        className={decision === 'skip' ? 'is-selected' : ''}
                        aria-pressed={decision === 'skip'}
                        onClick={() => decide(item.id, 'skip')}
                        disabled={busy}
                      >
                        Пропустить
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        icon={RefreshCw}
                        className={decision === 'replace' ? 'is-selected' : ''}
                        aria-pressed={decision === 'replace'}
                        onClick={() => decide(item.id, 'replace')}
                        disabled={busy || Boolean(replaceBlocked)}
                        title={replaceTitle}
                      >
                        Заменить трек
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        icon={Check}
                        className={decision === 'save' ? 'is-selected' : ''}
                        aria-pressed={decision === 'save'}
                        onClick={() => decide(item.id, 'save')}
                        disabled={busy}
                      >
                        Сохранить копию
                      </Button>
                    </>
                  )}
                </div>
              </li>
            );
          })}
        </ul>
      )}

      <p className="decisions-hint">
        Escape и закрытие диалога ничего не подтверждают. Файл попадает в медиатеку только после
        «Применить решения».
      </p>
    </Modal>
  );
}
