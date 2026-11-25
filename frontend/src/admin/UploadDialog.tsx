import { useMemo, useState } from 'react';
import {
  Check,
  CircleAlert,
  Clock3,
  ListChecks,
  RefreshCw,
  SkipForward,
  Upload,
  X,
} from 'lucide-react';
import { Button, ProgressBar, Spinner } from '../components/ui';
import { pluralRu } from '../lib/format';
import { Modal } from '../components/Modal';
import { InlineError } from '../components/States';
import { useUploads } from '../lib/uploads';
import type { LocalUploadItem } from '../lib/uploads';
import type { UploadBatchItem, UploadItemState } from '../lib/types';
import { duplicateReasonLabel } from '../lib/media';
import { formatBytes } from '../lib/format';
import { UPLOAD_ACCEPT } from '../lib/upload';

type UploadFilter = 'all' | 'matches' | 'added' | 'errors';

const STATE_LABELS: Record<UploadItemState, string> = {
  queued: 'Ожидает',
  uploading: 'Загружается',
  processing: 'Обрабатывается',
  added: 'Добавлен',
  pending: 'Требует решения',
  saved: 'Сохранён как дубликат',
  skipped: 'Пропущен',
  cancelled: 'Отменён',
  error: 'Ошибка',
};

const FILTERS: Array<{ value: UploadFilter; label: string }> = [
  { value: 'all', label: 'Все' },
  { value: 'matches', label: 'Совпадения' },
  { value: 'added', label: 'Добавленные' },
  { value: 'errors', label: 'Ошибки' },
];

function statusIcon(state: UploadItemState | LocalUploadItem['status']) {
  if (state === 'added' || state === 'saved') return <Check size={14} aria-hidden="true" />;
  if (state === 'pending') return <ListChecks size={14} aria-hidden="true" />;
  if (state === 'error') return <CircleAlert size={14} aria-hidden="true" />;
  if (state === 'uploading' || state === 'processing') return <Spinner size={13} />;
  if (state === 'skipped') return <SkipForward size={14} aria-hidden="true" />;
  if (state === 'cancelled') return <X size={14} aria-hidden="true" />;
  return <Clock3 size={14} aria-hidden="true" />;
}

function statusTone(state: string): string {
  if (state === 'added' || state === 'saved') return 'is-ok';
  if (state === 'pending') return 'is-warn';
  if (state === 'error') return 'is-error';
  return '';
}

interface UploadRow {
  key: string;
  filename: string;
  size: number | null;
  state: UploadItemState | LocalUploadItem['status'];
  error: string | null;
  decision: UploadBatchItem['decision'];
  reason: UploadBatchItem['reason'];
  local: LocalUploadItem | null;
  server: UploadBatchItem | null;
}

interface UploadDialogProps {
  open: boolean;
  onClose: () => void;
  onOpenDecisions: (pendingId?: string) => void;
  maxUploadMb: number;
  settingsUnavailable: boolean;
}

export function UploadDialog({
  open,
  onClose,
  onOpenDecisions,
  maxUploadMb,
  settingsUnavailable,
}: UploadDialogProps) {
  const {
    batch,
    batchError,
    localItems,
    uploading,
    addFiles,
    retry,
    cancelBatch,
    clearBatch,
  } = useUploads();
  const [filter, setFilter] = useState<UploadFilter>('all');

  const pending = batch?.items.filter((item) => item.state === 'pending' && !item.decision) ?? [];

  const rows = useMemo<UploadRow[]>(() => {
    const localRows: UploadRow[] = localItems.map((item) => ({
      key: `local-${item.key}`,
      filename: item.file.name,
      size: item.file.size,
      state: item.status,
      error: item.error,
      decision: null,
      reason: null,
      local: item,
      server: null,
    }));
    const serverRows: UploadRow[] = (batch?.items ?? []).map((item) => ({
      key: `server-${item.id}`,
      filename: item.filename,
      size: item.size,
      state: item.state,
      error: item.error,
      decision: item.decision,
      reason: item.reason,
      local: null,
      server: item,
    }));
    const combined = [...localRows, ...serverRows];
    switch (filter) {
      case 'matches':
        return combined.filter((row) => row.state === 'pending' && !row.decision);
      case 'added':
        return combined.filter((row) => row.state === 'added' || row.state === 'saved');
      case 'errors':
        return combined.filter((row) => row.state === 'error');
      default:
        return combined;
    }
  }, [batch, localItems, filter]);

  const totalBytes =
    localItems.reduce((sum, item) => sum + item.file.size, 0) +
    (batch?.items.reduce((sum, item) => sum + (item.size ?? 0), 0) ?? 0);
  const settledBytes =
    batch?.items.reduce((sum, item) => {
      if (item.state === 'queued' || item.state === 'uploading') return sum;
      return sum + (item.size ?? 0);
    }, 0) ?? 0;
  const uploadingBytes = localItems.reduce(
    (sum, item) =>
      item.status === 'uploading' ? sum + (item.file.size * item.progress) / 100 : sum,
    0,
  );
  const progress = totalBytes > 0 ? Math.min(1, (settledBytes + uploadingBytes) / totalBytes) : 0;
  const totalFiles = (batch?.items.length ?? 0) + localItems.length;
  const settledFiles =
    (batch?.items.filter((item) => item.state !== 'queued' && item.state !== 'uploading').length ??
      0) + localItems.filter((item) => item.status === 'error').length;

  const added = batch?.counts.added ?? 0;
  const needsDecision = batch?.counts.pending ?? 0;
  const errors = (batch?.counts.errors ?? 0) + localItems.filter((item) => item.status === 'error').length;
  const active = uploading || (batch?.counts.pending ?? 0) > 0;

  const close = () => {
    onClose();
  };

  const finish = () => {
    clearBatch();
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title="Загрузка музыки"
      size="lg"
      description={
        active
          ? 'Диалог можно закрыть — загрузка и обработка продолжатся, вернуться к ним можно из медиатеки.'
          : `MP3, FLAC, OGG, M4A · до ${maxUploadMb} МБ на файл${
              settingsUnavailable ? ' · лимит из настроек недоступен, показано значение по умолчанию' : ''
            }`
      }
      footer={
        <>
          <div className="upload-footer__actions">
            {needsDecision > 0 && !uploading && (
              <Button
                variant="secondary"
                icon={ListChecks}
                onClick={() => onOpenDecisions(pending[0]?.id)}
              >
                Просмотреть совпадения ({needsDecision})
              </Button>
            )}
            {active ? (
              <Button variant="ghost" icon={X} onClick={() => void cancelBatch()}>
                Отменить
              </Button>
            ) : totalFiles > 0 ? (
              <Button variant="ghost" onClick={finish}>
                Скрыть
              </Button>
            ) : null}
          </div>
        </>
      }
    >
      <div className="upload-progress" role="group" aria-label="Общий прогресс загрузки">
        <div className="upload-progress__head">
          <span>
            {uploading ? (
              <span className="inline-loading">
                <Spinner size={14} /> Загрузка…
              </span>
            ) : totalFiles > 0 ? (
              `Обработано ${settledFiles} из ${totalFiles}`
            ) : (
              'Файлы ещё не выбраны'
            )}
          </span>
          <span className="upload-progress__percent">{Math.round(progress * 100)}%</span>
        </div>
        <ProgressBar value={progress} label="Общий прогресс загрузки" />
        <div className="upload-counters">
          <span className="upload-footer__count is-ok">Добавлено: {added}</span>
          <span className="upload-footer__count is-warn">Требуют решения: {needsDecision}</span>
          <span className="upload-footer__count is-error">Ошибки: {errors}</span>
        </div>
      </div>

      {needsDecision > 0 && (
        <div className="upload-matches" role="status">
          <span className="upload-matches__icon">
            <ListChecks size={18} aria-hidden="true" />
          </span>
          <div className="upload-matches__text">
            <strong>Обнаружено {needsDecision}{' '}{pluralRu(needsDecision, ['совпадение', 'совпадения', 'совпадений'])}</strong>
            <span>Файлы сохранены во временном хранилище и ждут решения.</span>
          </div>
          <Button
            variant="secondary"
            size="sm"
            icon={ListChecks}
            onClick={() => onOpenDecisions(pending[0]?.id)}
          >
            Просмотреть совпадения ({needsDecision})
          </Button>
        </div>
      )}

      <label className="dropzone">
        <input
          type="file"
          multiple
          accept={UPLOAD_ACCEPT}
          className="visually-hidden"
          aria-label="Выбрать аудиофайлы для загрузки"
          onChange={(event) => {
            addFiles(Array.from(event.target.files ?? []));
            event.target.value = '';
          }}
        />
        <Upload size={22} aria-hidden="true" />
        <span className="dropzone__title">Перетащите файлы сюда или выберите на диске</span>
        <span className="dropzone__hint">
          Новый файл всегда становится отдельной записью: совпадение по хешу или тегам не заменит
          существующий трек.
        </span>
      </label>

      {batchError && <InlineError message={batchError} />}

      <div className="upload-filters" role="tablist" aria-label="Фильтр файлов загрузки">
        {FILTERS.map((entry) => (
          <button
            key={entry.value}
            type="button"
            role="tab"
            aria-selected={filter === entry.value}
            className={`upload-filter ${filter === entry.value ? 'is-active' : ''}`.trim()}
            onClick={() => setFilter(entry.value)}
          >
            {entry.label}
            {entry.value === 'matches' && needsDecision > 0 ? ` (${needsDecision})` : ''}
          </button>
        ))}
      </div>

      {rows.length === 0 ? (
        <p className="upload-empty">
          {totalFiles === 0
            ? 'Выберите аудиофайлы — они появятся здесь с состоянием каждого файла.'
            : 'В этом фильтре пока нет файлов.'}
        </p>
      ) : (
        <ul className="upload-list upload-list--dialog">
          {rows.map((row) => (
            <li key={row.key} className={`upload-item upload-item--${row.state}`}>
              <span className={`upload-item__icon ${statusTone(row.state)}`.trim()} aria-hidden="true">
                {statusIcon(row.state)}
              </span>
              <div className="upload-item__main">
                <span className="upload-item__name" title={row.filename}>
                  {row.filename}
                </span>
                <span className="upload-item__meta">
                  {formatBytes(row.size)}
                  {row.reason && row.state === 'pending' ? ` · ${duplicateReasonLabel(row.reason)}` : ''}
                  {row.error ? ` · ${row.error}` : ''}
                </span>
              </div>
              {row.state === 'uploading' && row.local ? (
                <div className="upload-item__progress">
                  <ProgressBar value={row.local.progress / 100} label={`Загрузка ${row.filename}`} />
                  <span>{row.local.progress}%</span>
                </div>
              ) : (
                <span className={`upload-item__status ${statusTone(row.state)}`.trim()}>
                  {row.state === 'pending' && row.decision
                    ? row.decision === 'save'
                      ? 'Сохранён как дубликат'
                      : 'Пропущен'
                    : STATE_LABELS[row.state as UploadItemState]}
                </span>
              )}
              <div className="upload-item__actions">
                {row.server && row.server.state === 'pending' && !row.server.decision && (
                  <Button
                    variant="secondary"
                    size="sm"
                    onClick={() => onOpenDecisions(row.server?.id)}
                  >
                    Проверить совпадение
                  </Button>
                )}
                {row.local && row.local.status === 'error' && row.local.error !== 'Загрузка отменена' && (
                  <Button
                    variant="ghost"
                    size="sm"
                    icon={RefreshCw}
                    onClick={() => retry(row.local?.key ?? '')}
                  >
                    Повторить
                  </Button>
                )}
              </div>
            </li>
          ))}
        </ul>
      )}
    </Modal>
  );
}
