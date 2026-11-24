import type { ReactNode } from 'react';
import { AlertTriangle, Inbox, RefreshCw, WifiOff } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Button, Spinner } from './ui';

export function LoadingState({ label = 'Загрузка…', rows = 3 }: { label?: string; rows?: number }) {
  return (
    <div className="state state--loading" role="status" aria-live="polite">
      <div className="state__loading-head">
        <Spinner />
        <span>{label}</span>
      </div>
      <div className="skeleton-list" aria-hidden="true">
        {Array.from({ length: rows }).map((_, index) => (
          <span key={index} className="skeleton" style={{ width: `${92 - index * 12}%` }} />
        ))}
      </div>
    </div>
  );
}

export function EmptyState({
  title,
  description,
  icon: Icon = Inbox,
  action,
  compact = false,
}: {
  title: string;
  description?: ReactNode;
  icon?: LucideIcon;
  action?: ReactNode;
  compact?: boolean;
}) {
  return (
    <div className={`state state--empty ${compact ? 'state--compact' : ''}`.trim()}>
      <span className="state__icon" aria-hidden="true">
        <Icon size={compact ? 20 : 26} />
      </span>
      <div className="state__text">
        <p className="state__title">{title}</p>
        {description && <p className="state__desc">{description}</p>}
      </div>
      {action && <div className="state__action">{action}</div>}
    </div>
  );
}

export function ErrorState({
  message,
  onRetry,
  title = 'Не удалось загрузить данные',
}: {
  message: string;
  onRetry?: () => void;
  title?: string;
}) {
  return (
    <div className="state state--error" role="alert">
      <span className="state__icon" aria-hidden="true">
        <AlertTriangle size={24} />
      </span>
      <div className="state__text">
        <p className="state__title">{title}</p>
        <p className="state__desc">{message}</p>
      </div>
      {onRetry && (
        <div className="state__action">
          <Button variant="secondary" size="sm" icon={RefreshCw} onClick={onRetry}>
            Повторить
          </Button>
        </div>
      )}
    </div>
  );
}

export function ServiceUnavailable({
  title = 'Служба недоступна',
  message,
}: {
  title?: string;
  message: string;
}) {
  return (
    <div className="banner banner--warning" role="status">
      <WifiOff size={16} aria-hidden="true" />
      <div>
        <strong>{title}</strong>
        <p>{message}</p>
      </div>
    </div>
  );
}

export function InlineError({ message }: { message: string }) {
  return (
    <p className="inline-error" role="alert">
      <AlertTriangle size={14} aria-hidden="true" />
      <span>{message}</span>
    </p>
  );
}
