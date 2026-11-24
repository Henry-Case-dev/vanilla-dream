import type { StationStatus } from '../lib/types';
import { statusText } from '../lib/format';

const toneByStatus: Record<StationStatus, string> = {
  on_air: 'is-live',
  starting: 'is-pending',
  reconnecting: 'is-pending',
  no_music: 'is-idle',
  error: 'is-error',
  off: 'is-idle',
  unknown: 'is-idle',
};

interface StatusPillProps {
  status: StationStatus;
  label?: string | null;
  className?: string;
}

export function StatusPill({ status, label, className = '' }: StatusPillProps) {
  const text = label && label.trim() ? label : statusText(status);
  return (
    <span className={`status-pill ${toneByStatus[status]} ${className}`.trim()}>
      <span className="status-pill__dot" aria-hidden="true" />
      <span>{text}</span>
    </span>
  );
}
