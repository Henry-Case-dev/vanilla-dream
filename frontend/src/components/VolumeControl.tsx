interface VolumeControlProps {
  value: number;
  onChange: (value: number) => void;
  disabled?: boolean;
  className?: string;
}

import { Volume1, Volume2, VolumeX } from 'lucide-react';

/** Громкость только локального прослушивания потока. */
export function VolumeControl({ value, onChange, disabled = false, className = '' }: VolumeControlProps) {
  const muted = value <= 0.001;
  const Icon = muted ? VolumeX : value < 0.5 ? Volume1 : Volume2;
  return (
    <div className={`volume ${className}`.trim()}>
      <button
        type="button"
        className="icon-btn icon-btn--sm"
        aria-label={muted ? 'Включить звук' : 'Выключить звук'}
        title={muted ? 'Включить звук' : 'Выключить звук'}
        disabled={disabled}
        onClick={() => onChange(muted ? 0.8 : 0)}
      >
        <Icon size={16} aria-hidden="true" />
      </button>
      <input
        type="range"
        className="volume__range"
        min={0}
        max={1}
        step={0.01}
        value={value}
        disabled={disabled}
        aria-label="Громкость локального плеера"
        onChange={(event) => onChange(Number(event.target.value))}
        style={{ ['--volume' as string]: `${Math.round(value * 100)}%` }}
      />
    </div>
  );
}
