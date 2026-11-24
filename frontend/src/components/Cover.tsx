import { useEffect, useId, useState } from 'react';

interface CoverProps {
  src?: string | null;
  alt: string;
  className?: string;
  size?: 'xs' | 'sm' | 'md' | 'lg' | 'hero' | 'fill';
  rounded?: 'sm' | 'md' | 'lg' | 'full';
}

const sizeClass: Record<NonNullable<CoverProps['size']>, string> = {
  xs: 'cover--xs',
  sm: 'cover--sm',
  md: 'cover--md',
  lg: 'cover--lg',
  hero: 'cover--hero',
  fill: 'cover--fill',
};

/** Обложка трека с локальной SVG-заглушкой, если обложки нет или она не загрузилась. */
export function Cover({ src, alt, className = '', size = 'md', rounded = 'md' }: CoverProps) {
  const [failed, setFailed] = useState(false);
  const gradientId = `cover-grad-${useId().replace(/[^a-zA-Z0-9_-]/g, '')}`;
  const classes = ['cover', sizeClass[size], `cover--r-${rounded}`, className].filter(Boolean).join(' ');
  const showImage = Boolean(src) && !failed;

  useEffect(() => {
    setFailed(false);
  }, [src]);

  return (
    <div className={classes} data-empty={showImage ? undefined : 'true'}>
      {showImage ? (
        <img
          src={src ?? undefined}
          alt={alt}
          loading="lazy"
          decoding="async"
          onError={() => setFailed(true)}
        />
      ) : (
        <span className="cover__placeholder" aria-hidden="true">
          <svg viewBox="0 0 64 64" width="100%" height="100%" role="presentation" focusable="false">
            <defs>
              <linearGradient id={gradientId} x1="0" y1="0" x2="1" y2="1">
                <stop offset="0%" stopColor="#2A2437" />
                <stop offset="100%" stopColor="#1A1823" />
              </linearGradient>
            </defs>
            <rect width="64" height="64" fill={`url(#${gradientId})`} />
            <g stroke="#BCA1F4" strokeWidth="2" strokeLinecap="round" opacity="0.75">
              <path d="M20 40V28" />
              <path d="M26.5 44V20" />
              <path d="M33 40V26" />
              <path d="M39.5 44V22" />
              <path d="M46 38V30" />
            </g>
          </svg>
        </span>
      )}
    </div>
  );
}
