interface LogoProps {
  compact?: boolean;
  className?: string;
}

/**
 * Собственный знак станции: эквалайзер + радиоволна.
 * Никаких внешних изображений и шрифтов — только SVG и локальный Inter.
 */
export function Logo({ compact = false, className = '' }: LogoProps) {
  return (
    <span className={`logo ${compact ? 'logo--compact' : ''} ${className}`.trim()}>
      <span className="logo__mark" aria-hidden="true">
        <svg viewBox="0 0 40 40" width="100%" height="100%" role="presentation" focusable="false">
          <rect x="0" y="0" width="40" height="40" rx="11" fill="var(--accent-selected)" />
          <path
            d="M12 24.5a8 8 0 0 1 16 0"
            fill="none"
            stroke="var(--accent)"
            strokeWidth="1.6"
            strokeLinecap="round"
            opacity="0.55"
          />
          <g stroke="var(--accent)" strokeWidth="2.6" strokeLinecap="round">
            <path d="M13.5 22.5v-5" />
            <path d="M19 25.5V14.5" />
            <path d="M24.5 23v-7" />
            <path d="M30 21v-3" />
          </g>
          <circle cx="30.5" cy="11.5" r="1.8" fill="var(--accent-hover)" />
        </svg>
      </span>
      <span className="logo__text">
        <span className="logo__name">vanilla dream</span>
        {!compact && <span className="logo__sub">RADIO</span>}
      </span>
    </span>
  );
}
