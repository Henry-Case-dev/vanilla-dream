import { Archive, Check, TriangleAlert, X } from 'lucide-react';
import { Button } from '../components/ui';
import { Modal } from '../components/Modal';
import { useExports } from '../lib/exports';
import { ExportStatusBody } from './ExportDialog';

export function ExportIndicator() {
  const { job, error, statusOpen, cancelJob, dismiss, openStatus, closeStatus } = useExports();

  if (!job && !error) return null;

  const active =
    job !== null && ['queued', 'preparing', 'downloading'].includes(job.state);

  let label = 'Экспорт';
  let icon = <Archive size={13} aria-hidden="true" />;
  let tone = '';
  if (error && !job) {
    label = 'Экспорт недоступен';
    icon = <TriangleAlert size={13} aria-hidden="true" />;
    tone = 'is-error';
  } else if (job) {
    switch (job.state) {
      case 'ready':
        label = 'Архив готов';
        icon = <Check size={13} aria-hidden="true" />;
        tone = 'is-ready';
        break;
      case 'error':
        label = 'Ошибка экспорта';
        icon = <TriangleAlert size={13} aria-hidden="true" />;
        tone = 'is-error';
        break;
      case 'cancelled':
        label = 'Экспорт отменён';
        tone = 'is-muted';
        break;
      default:
        label =
          job.total > 0
            ? `Архив: ${job.processed} из ${job.total}`
            : 'Готовим архив…';
    }
  }

  return (
    <>
      <div className="export-chip-row">
        <button
          type="button"
          className={`export-chip ${tone}`.trim()}
          onClick={openStatus}
          aria-label={`${label}. Открыть состояние экспорта`}
        >
          {icon}
          <span>{label}</span>
        </button>
      </div>

      <Modal
        open={statusOpen}
        onClose={closeStatus}
        title="Экспорт"
        size="sm"
        footer={
          <>
            <Button variant="ghost" onClick={dismiss} disabled={active}>
              Скрыть
            </Button>
            {active && (
              <Button variant="ghost" icon={X} onClick={() => void cancelJob()}>
                Отменить
              </Button>
            )}
          </>
        }
      >
        <ExportStatusBody job={job} error={error} onCancel={() => void cancelJob()} />
      </Modal>
    </>
  );
}
