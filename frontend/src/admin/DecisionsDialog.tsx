import { useEffect, useMemo, useRef, useState } from 'react';
import { ArrowLeft, Check, ChevronDown, ListChecks, Search, SkipForward } from 'lucide-react';
import { Button, Chip, Spinner, TextField } from '../components/ui';
import { Modal } from '../components/Modal';
import { EmptyState, InlineError } from '../components/States';
import { Cover } from '../components/Cover';
import { useToast } from '../components/Toasts';
import { api, errorMessage } from '../lib/api';
import { formatBytes, formatDuration } from '../lib/format';
import { duplicateReasonLabel } from '../lib/media';
import { notifyDataChanged } from '../lib/refresh';
import type { DecisionAction, TrackComparison, UploadBatchItem } from '../lib/types';

interface DecisionsDialogProps {
  open: boolean;
  items: UploadBatchItem[];
  uploading: boolean;
  initialPendingId?: string | null;
  onClose: () => void;
  onBack: () => void;
  onChanged: () => Promise<void>;
}

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
  const [search, setSearch] = useState('');
  const [decisions, setDecisions] = useState<Record<string, DecisionAction>>({});
  const [expanded, setExpanded] = useState<Set<string>>(new Set());
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const listRef = useRef<HTMLUListElement | null>(null);

  useEffect(() => {
    if (!open) return;
    setSearch('');
    setDecisions({});
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

  const pendingItems = useMemo(
    () => items.filter((item) => item.state === 'pending'),
    [items],
  );

  const effective = (item: UploadBatchItem): DecisionAction | null =>
    decisions[item.id] ?? item.decision;

  const remaining = pendingItems.filter((item) => !effective(item)).length;
  const saves = pendingItems.filter((item) => effective(item) === 'save').length;
  const skips = pendingItems.filter((item) => effective(item) === 'skip').length;

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

  const decide = (id: string, action: DecisionAction) => {
    setDecisions((current) => ({ ...current, [id]: action }));
  };

  const decideRemaining = (action: DecisionAction) => {
    setDecisions((current) => {
      const next = { ...current };
      pendingItems.forEach((item) => {
        if (!effective(item)) next[item.id] = action;
      });
      return next;
    });
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

  const apply = async () => {
    if (remaining > 0) return;
    const entries = pendingItems
      .map((item) => ({ item, action: effective(item) }))
      .filter((entry): entry is { item: UploadBatchItem; action: DecisionAction } =>
        entry.action !== null,
      );
    if (entries.length === 0) {
      onClose();
      return;
    }
    setBusy(true);
    setError(null);
    try {
      for (const entry of entries) {
        await api.admin.uploads.decide(entry.item.id, entry.action);
      }
      toast.notify(
        'success',
        `Решения применены: сохранено ${entries.filter((entry) => entry.action === 'save').length}, пропущено ${
          entries.filter((entry) => entry.action === 'skip').length
        }`,
      );
      notifyDataChanged();
      await onChanged();
      onClose();
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

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
              disabled={remaining > 0 || pendingItems.length === 0}
              title={remaining > 0 ? 'Сначала примите решение по каждому файлу' : undefined}
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
            icon={Check}
            disabled={remaining === 0 || busy}
            onClick={() => decideRemaining('save')}
          >
            Сохранить оставшиеся
          </Button>
        </div>
      </div>

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

      {pendingItems.length === 0 ? (
        <EmptyState
          compact
          icon={ListChecks}
          title="Совепадений не осталось"
          description="Все решения по этой загрузке уже приняты."
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
            const match = item.matches[0] ?? null;
            const otherMatches = item.matches.slice(1);
            const isOpen = expanded.has(item.id);
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
                      <CompareColumn title="В медиатеке" data={match?.track ?? null} other={item.metadata} />
                      <CompareColumn title="Новый файл" data={item.metadata} other={match?.track ?? null} />
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

                <div className="decision__actions">
                  {item.decision ? (
                    <span className="decision__decided">
                      {item.decision === 'save' ? 'Сохранён как дубликат' : 'Пропущен'}
                    </span>
                  ) : (
                    <>
                      <Button
                        variant={decision === 'skip' ? 'danger' : 'secondary'}
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
                        variant={decision === 'save' ? 'primary' : 'secondary'}
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
