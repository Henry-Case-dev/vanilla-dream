import { useEffect, useState } from 'react';
import {
  Archive,
  Check,
  Download,
  Files,
  Music4,
  X,
} from 'lucide-react';
import { Button, ProgressBar, Spinner } from '../components/ui';
import { Cover } from '../components/Cover';
import { Modal } from '../components/Modal';
import { EmptyState, InlineError } from '../components/States';
import { useToast } from '../components/Toasts';
import { api, errorMessage, exportArchiveUrl } from '../lib/api';
import { useExports } from '../lib/exports';
import { triggerDownload, triggerDownloads } from '../lib/download';
import { formatBytes, tracksLabel } from '../lib/format';
import type {
  ExportComposition,
  ExportFile,
  ExportFormat,
  ExportJob,
  ExportMetadataMode,
} from '../lib/types';

function exportTitle(composition: ExportComposition): string {
  switch (composition.scope) {
    case 'playlist':
      return 'Экспорт плейлиста';
    case 'tracks':
      return 'Экспорт выбранных треков';
    default:
      return 'Экспорт медиатеки';
  }
}

export function ExportStatusBody({
  job,
  error,
  onCancel,
}: {
  job: ExportJob | null;
  error: string | null;
  onCancel: () => void;
}) {
  if (error && !job) return <InlineError message={error} />;
  if (!job) {
    return (
      <p className="export-status__wait">
        <Spinner size={14} /> Готовим задание экспорта…
      </p>
    );
  }

  if (job.state === 'ready') {
    return (
      <div className="export-ready" role="status">
        <span className="export-ready__icon">
          <Check size={20} aria-hidden="true" />
        </span>
        <div className="export-ready__text">
          <strong>{job.archive?.name ?? 'Архив готов'}</strong>
          <span>
            {job.archive ? formatBytes(job.archive.size) : 'Архив готов к скачиванию'} ·{' '}
            {tracksLabel(job.total)}
          </span>
        </div>
        <a
          className="btn btn--primary btn--md"
          href={exportArchiveUrl(job.id)}
          download
          onClick={(event) => {
            event.preventDefault();
            triggerDownload(exportArchiveUrl(job.id));
          }}
        >
          <Download size={16} aria-hidden="true" />
          <span className="btn__label">Скачать архив</span>
        </a>
      </div>
    );
  }

  if (job.state === 'cancelled') {
    return (
      <div className="export-status__final">
        <strong>Экспорт отменён</strong>
        <span>Временные файлы удалены.</span>
      </div>
    );
  }

  if (job.state === 'error') {
    return (
      <div className="export-status__final is-error" role="alert">
        <strong>Не удалось подготовить архив</strong>
        <span>{job.error ?? 'Повторите экспорт позже.'}</span>
      </div>
    );
  }

  const progress =
    job.total > 0 ? job.processed / job.total : job.progress > 0 ? job.progress : 0;
  return (
    <div className="export-progress" role="status">
      <div className="export-progress__head">
        <span>
          Подготавливаем архив: {job.processed} из {job.total || '…'}
        </span>
        <span>{Math.round(progress * 100)}%</span>
      </div>
      <ProgressBar value={progress} label="Подготовка архива" />
      <p className="export-progress__note">
        Архив собирается на сервере. Можно закрыть диалог и вернуться к нему из индикатора в
        верхней части панели.
      </p>
      <Button variant="ghost" size="sm" icon={X} onClick={onCancel}>
        Отменить подготовку
      </Button>
    </div>
  );
}

function Composition({
  composition,
}: {
  composition: ExportComposition;
}) {
  return (
    <div className="export-composition">
      {composition.coverUrl ? (
        <Cover src={composition.coverUrl} alt="" size="lg" rounded="md" className="export-composition__cover" />
      ) : (
        <span className="export-composition__placeholder" aria-hidden="true">
          <Music4 size={22} />
        </span>
      )}
      <div className="export-composition__text">
        <strong>{composition.title}</strong>
        <span>
          {tracksLabel(composition.count)} ·{' '}
          {composition.size === null
            ? 'размер уточняется'
            : `${composition.partialSize ? '≈ ' : ''}${formatBytes(composition.size)}`}
        </span>
      </div>
    </div>
  );
}

interface ExportDialogProps {
  open: boolean;
  composition: ExportComposition | null;
  onClose: () => void;
}

export function ExportDialog({ open, composition, onClose }: ExportDialogProps) {
  const toast = useToast();
  const { job, error: jobError, startJob, cancelJob } = useExports();
  const [format, setFormat] = useState<ExportFormat>('zip');
  const [metadata, setMetadata] = useState<ExportMetadataMode>('tags');
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [files, setFiles] = useState<ExportFile[]>([]);
  const [createdJobId, setCreatedJobId] = useState<string | null>(null);

  useEffect(() => {
    if (!open) return;
    setFormat('zip');
    setMetadata('tags');
    setError(null);
    setFiles([]);
    setCreatedJobId(null);
  }, [open]);

  if (!composition) return null;

  const activeJob = job && createdJobId && job.id === createdJobId ? job : null;
  const jobView = createdJobId !== null;

  const create = async () => {
    if (composition.count === 0) {
      setError('В выбранном составе нет треков.');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      const result = await api.admin.exports.create({
        scope: composition.scope,
        ids: composition.scope === 'tracks' ? composition.ids : undefined,
        playlist_id: composition.scope === 'playlist' ? composition.playlistId : undefined,
        format,
        metadata,
      });
      if (result.kind === 'job') {
        setCreatedJobId(result.job.id);
        startJob(result.job.id);
      } else {
        setFiles(result.files);
        if (result.files.length === 0) {
          setError('Сервер не вернул файлы для скачивания.');
        } else {
          toast.notify('info', 'Файлы готовы к скачиванию');
        }
      }
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const close = () => {
    if (busy) return;
    onClose();
  };

  return (
    <Modal
      open={open}
      onClose={close}
      title={exportTitle(composition)}
      description={
        jobView
          ? 'Задание выполняется на сервере. Закрытие диалога его не отменяет.'
          : 'Архив готовится на сервере; браузер не держит файлы в памяти.'
      }
      footer={
        jobView ? (
          <>
            <Button variant="ghost" onClick={close}>
              Закрыть
            </Button>
            {activeJob && ['queued', 'preparing', 'downloading'].includes(activeJob.state) && (
              <Button variant="ghost" icon={X} onClick={() => void cancelJob()}>
                Отменить
              </Button>
            )}
          </>
        ) : files.length > 0 ? (
          <Button variant="secondary" onClick={close}>
            Закрыть
          </Button>
        ) : (
          <>
            <Button variant="ghost" onClick={close} disabled={busy}>
              Отмена
            </Button>
            <Button
              variant="primary"
              icon={Download}
              onClick={() => void create()}
              loading={busy}
              disabled={composition.count === 0}
            >
              {format === 'zip' ? 'Подготовить архив' : 'Скачать файлы'}
            </Button>
          </>
        )
      }
    >
      <Composition composition={composition} />

      {jobView ? (
        <ExportStatusBody job={activeJob ?? job} error={jobError} onCancel={() => void cancelJob()} />
      ) : files.length > 0 ? (
        <div className="export-files">
          <div className="export-files__head">
            <strong>Файлы готовы</strong>
            <Button
              variant="secondary"
              size="sm"
              icon={Download}
              onClick={() => triggerDownloads(files.map((file) => file.url))}
            >
              Скачать все файлы
            </Button>
          </div>
          <ul className="export-files__list">
            {files.map((file) => (
              <li key={file.trackId}>
                <span className="export-files__name" title={file.name}>
                  {file.name}
                </span>
                <span className="export-files__size">{formatBytes(file.size)}</span>
                <Button
                  variant="ghost"
                  size="sm"
                  icon={Download}
                  aria-label={`Скачать ${file.name}`}
                  onClick={() => triggerDownload(file.url)}
                >
                  Скачать
                </Button>
              </li>
            ))}
          </ul>
          <p className="export-files__note">
            Если браузер спросит разрешение на несколько загрузок, разрешите их для этого сайта.
            Файлы не скачиваются автоматически — только по кнопке.
          </p>
        </div>
      ) : (
        <>
          <div className="export-section" role="radiogroup" aria-label="Формат экспорта">
            <span className="export-section__title">Формат</span>
            <button
              type="button"
              role="radio"
              aria-checked={format === 'zip'}
              className={`export-option ${format === 'zip' ? 'is-selected' : ''}`.trim()}
              onClick={() => setFormat('zip')}
            >
              <span className="export-option__icon">
                <Archive size={18} aria-hidden="true" />
              </span>
              <span className="export-option__text">
                <strong>ZIP-архив</strong>
                <span>Один архив с треками и playlist.m3u8 для плейлиста.</span>
              </span>
              <span className="export-option__check" aria-hidden="true">
                {format === 'zip' && <Check size={14} />}
              </span>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={format === 'files'}
              className={`export-option ${format === 'files' ? 'is-selected' : ''}`.trim()}
              onClick={() => setFormat('files')}
            >
              <span className="export-option__icon">
                <Files size={18} aria-hidden="true" />
              </span>
              <span className="export-option__text">
                <strong>Отдельными файлами</strong>
                <span>Список файлов с отдельным скачиванием каждого.</span>
              </span>
              <span className="export-option__check" aria-hidden="true">
                {format === 'files' && <Check size={14} />}
              </span>
            </button>
          </div>

          <div className="export-section" role="radiogroup" aria-label="Режим метаданных">
            <span className="export-section__title">Метаданные</span>
            <button
              type="button"
              role="radio"
              aria-checked={metadata === 'tags'}
              className={`export-option ${metadata === 'tags' ? 'is-selected' : ''}`.trim()}
              onClick={() => setMetadata('tags')}
            >
              <span className="export-option__text">
                <strong>С текущими тегами</strong>
                <span>
                  В копию запишутся текущие название, исполнитель, альбом и обложка. Звук не
                  перекодируется.
                </span>
              </span>
              <span className="export-option__check" aria-hidden="true">
                {metadata === 'tags' && <Check size={14} />}
              </span>
            </button>
            <button
              type="button"
              role="radio"
              aria-checked={metadata === 'original'}
              className={`export-option ${metadata === 'original' ? 'is-selected' : ''}`.trim()}
              onClick={() => setMetadata('original')}
            >
              <span className="export-option__text">
                <strong>Оригинальные файлы</strong>
                <span>
                  Файлы скачаются без изменений — с тегами и обложкой, которые были при загрузке.
                </span>
              </span>
              <span className="export-option__check" aria-hidden="true">
                {metadata === 'original' && <Check size={14} />}
              </span>
            </button>
          </div>

          {error && <InlineError message={error} />}
          {composition.count === 0 && (
            <EmptyState
              compact
              icon={Music4}
              title="Нечего экспортировать"
              description="В выбранном составе пока нет треков."
              action={<Button variant="ghost" size="sm" onClick={close}>Закрыть</Button>}
            />
          )}
        </>
      )}
    </Modal>
  );
}
