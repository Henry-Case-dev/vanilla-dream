import { useEffect, useId, useRef } from 'react';
import type { ReactNode } from 'react';
import { createPortal } from 'react-dom';
import { X } from 'lucide-react';
import { Button } from './ui';

interface ModalProps {
  open: boolean;
  onClose: () => void;
  title: string;
  description?: string;
  children: ReactNode;
  footer?: ReactNode;
  size?: 'sm' | 'md' | 'lg';
  closeLabel?: string;
}

const FOCUSABLE_SELECTOR = [
  'a[href]',
  'button:not([disabled])',
  'input:not([disabled]):not([type="hidden"]):not([type="file"]):not(.visually-hidden)',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
  '[tabindex]:not([tabindex="-1"])',
].join(', ');

const BODY_FIELD_SELECTOR = [
  'input:not([type="hidden"]):not([type="file"]):not([disabled]):not(.visually-hidden)',
  'select:not([disabled])',
  'textarea:not([disabled])',
  '[contenteditable="true"]',
].join(', ');

function visibleFocusable(root: HTMLElement | null): HTMLElement[] {
  if (!root) return [];
  return Array.from(root.querySelectorAll<HTMLElement>(FOCUSABLE_SELECTOR)).filter((element) => {
    if (element.getAttribute('aria-hidden') === 'true') return false;
    if (element.tabIndex < 0) return false;
    return element.getClientRects().length > 0;
  });
}

/** Счётчик блокировок прокрутки: два одновременно открытых диалога не должны
 * восстановить overflow раньше времени. */
let scrollLockCount = 0;
let scrollLockValue = '';

function lockBodyScroll(): void {
  if (scrollLockCount === 0) {
    scrollLockValue = document.body.style.overflow;
    document.body.style.overflow = 'hidden';
  }
  scrollLockCount += 1;
}

function unlockBodyScroll(): void {
  scrollLockCount = Math.max(0, scrollLockCount - 1);
  if (scrollLockCount === 0) document.body.style.overflow = scrollLockValue;
}

export function Modal({
  open,
  onClose,
  title,
  description,
  children,
  footer,
  size = 'md',
  closeLabel = 'Закрыть',
}: ModalProps) {
  const titleId = useId();
  const panelRef = useRef<HTMLDivElement | null>(null);
  const onCloseRef = useRef(onClose);
  const focusInitialised = useRef(false);
  const openerRef = useRef<HTMLElement | null>(null);

  onCloseRef.current = onClose;

  useEffect(() => {
    if (!open) {
      focusInitialised.current = false;
      return;
    }

    openerRef.current =
      document.activeElement instanceof HTMLElement ? document.activeElement : null;
    lockBodyScroll();

    const handleKeyDown = (event: KeyboardEvent) => {
      if (event.key === 'Escape') {
        event.preventDefault();
        event.stopPropagation();
        onCloseRef.current();
        return;
      }
      if (event.key !== 'Tab') return;

      const panel = panelRef.current;
      if (!panel) return;
      const focusables = visibleFocusable(panel);
      if (focusables.length === 0) {
        event.preventDefault();
        panel.focus();
        return;
      }
      const first = focusables[0];
      const last = focusables[focusables.length - 1];
      const active = document.activeElement;
      if (event.shiftKey) {
        if (active === first || !panel.contains(active)) {
          event.preventDefault();
          last?.focus();
        }
      } else if (active === last || !panel.contains(active)) {
        event.preventDefault();
        first?.focus();
      }
    };

    document.addEventListener('keydown', handleKeyDown, true);

    // Фокус ставится один раз за открытие, синхронно: повторные рендеры
    // (ввод в контролируемое поле, SSE-обновления) не должны его перехватывать.
    if (!focusInitialised.current) {
      const panel = panelRef.current;
      const bodyField = Array.from(
        panel?.querySelectorAll<HTMLElement>(`.modal__body ${BODY_FIELD_SELECTOR}`) ?? [],
      ).find((element) => element.getClientRects().length > 0 && element.tabIndex >= 0);
      const footerButton = Array.from(
        panel?.querySelectorAll<HTMLElement>('.modal__footer button:not([disabled])') ?? [],
      ).find((element) => element.getClientRects().length > 0);
      focusInitialised.current = true;
      if (bodyField) bodyField.focus();
      else if (footerButton) footerButton.focus();
      else panel?.focus();
    }

    return () => {
      document.removeEventListener('keydown', handleKeyDown, true);
      unlockBodyScroll();
      const opener = openerRef.current;
      openerRef.current = null;
      focusInitialised.current = false;
      if (opener && opener.isConnected) {
        try {
          opener.focus();
        } catch {
          /* элемент мог стать недоступным — это не ошибка диалога */
        }
      }
    };
  }, [open]);

  if (!open) return null;

  return createPortal(
    <div className="modal" role="presentation">
      <div className="modal__backdrop" onClick={onClose} />
      <div
        className={`modal__panel modal__panel--${size}`}
        role="dialog"
        aria-modal="true"
        aria-labelledby={titleId}
        ref={panelRef}
        tabIndex={-1}
      >
        <header className="modal__head">
          <div>
            <h2 className="modal__title" id={titleId}>
              {title}
            </h2>
            {description && <p className="modal__desc">{description}</p>}
          </div>
          <button type="button" className="icon-btn icon-btn--sm" aria-label={closeLabel} onClick={onClose}>
            <X size={16} aria-hidden="true" />
          </button>
        </header>
        <div className="modal__body">{children}</div>
        {footer && <footer className="modal__footer">{footer}</footer>}
      </div>
    </div>,
    document.body,
  );
}

interface ConfirmDialogProps {
  open: boolean;
  title: string;
  description: ReactNode;
  confirmLabel?: string;
  cancelLabel?: string;
  danger?: boolean;
  busy?: boolean;
  onConfirm: () => void;
  onCancel: () => void;
}

export function ConfirmDialog({
  open,
  title,
  description,
  confirmLabel = 'Подтвердить',
  cancelLabel = 'Отмена',
  danger = false,
  busy = false,
  onConfirm,
  onCancel,
}: ConfirmDialogProps) {
  return (
    <Modal
      open={open}
      onClose={busy ? () => undefined : onCancel}
      title={title}
      size="sm"
      footer={
        <>
          <Button variant="ghost" onClick={onCancel} disabled={busy}>
            {cancelLabel}
          </Button>
          <Button variant={danger ? 'danger' : 'primary'} onClick={onConfirm} disabled={busy} loading={busy}>
            {confirmLabel}
          </Button>
        </>
      }
    >
      <div className="confirm-text">{description}</div>
    </Modal>
  );
}
