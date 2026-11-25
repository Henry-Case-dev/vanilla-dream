import type { StationStatus } from '../lib/types';
import { describeStationStatus, statusToneClass } from '../lib/status';

interface StatusPillProps {
  status: StationStatus;
  reason?: string | null;
  className?: string;
}

/**
 * Единая подпись состояния эфира для обзора, нижней панели, сайдбара,
 * публичного плеера и настроек. Зелёный цвет — только для реального on_air.
 */
export function StatusPill({ status, reason, className = '' }: StatusPillProps) {
  const presentation = describeStationStatus(status, reason);
  return (
    <span
      className={`status-pill ${statusToneClass(presentation.tone)} ${className}`.trim()}
      title={presentation.detail}
    >
      <span className="status-pill__dot" aria-hidden="true" />
      <span>{presentation.label}</span>
    </span>
  );
}
