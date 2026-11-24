import { useCallback, useEffect, useRef, useState } from 'react';
import { Check, Copy } from 'lucide-react';

export function useCopy(resetMs = 2000): { copied: boolean; copy: (text: string) => Promise<boolean> } {
  const [copied, setCopied] = useState(false);
  const timer = useRef<number | null>(null);

  useEffect(
    () => () => {
      if (timer.current !== null) window.clearTimeout(timer.current);
    },
    [],
  );

  const copy = useCallback(
    async (text: string) => {
      const ok = await copyToClipboard(text);
      if (!ok) return false;
      setCopied(true);
      if (timer.current !== null) window.clearTimeout(timer.current);
      timer.current = window.setTimeout(() => setCopied(false), resetMs);
      return true;
    },
    [resetMs],
  );

  return { copied, copy };
}

async function copyToClipboard(text: string): Promise<boolean> {
  try {
    if (navigator.clipboard && window.isSecureContext) {
      await navigator.clipboard.writeText(text);
      return true;
    }
  } catch {
    /* переходим к резервному способу */
  }
  try {
    const area = document.createElement('textarea');
    area.value = text;
    area.setAttribute('readonly', 'readonly');
    area.style.position = 'fixed';
    area.style.opacity = '0';
    document.body.appendChild(area);
    area.select();
    const ok = document.execCommand('copy');
    document.body.removeChild(area);
    return ok;
  } catch {
    return false;
  }
}

interface CopyButtonProps {
  value: string;
  label?: string;
  className?: string;
  showLabel?: boolean;
}

/** Кнопка копирования с обратной связью «Скопировано». */
export function CopyButton({ value, label = 'Копировать', className = '', showLabel = true }: CopyButtonProps) {
  const { copied, copy } = useCopy();
  return (
    <button
      type="button"
      className={`copy-btn ${copied ? 'is-copied' : ''} ${showLabel ? '' : 'copy-btn--icon'} ${className}`.trim()}
      onClick={() => {
        void copy(value);
      }}
      aria-label={copied ? 'Скопировано' : label}
      title={copied ? 'Скопировано' : label}
    >
      {copied ? <Check size={14} aria-hidden="true" /> : <Copy size={14} aria-hidden="true" />}
      {showLabel && <span>{copied ? 'Скопировано' : label}</span>}
    </button>
  );
}
