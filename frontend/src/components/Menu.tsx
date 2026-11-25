import { useEffect, useRef, useState } from 'react';
import { createPortal } from 'react-dom';
import { ChevronDown, MoreHorizontal } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

export interface MenuItem {
  label: string;
  onSelect: () => void;
  icon?: LucideIcon;
  danger?: boolean;
  disabled?: boolean;
  hint?: string;
  separatorBefore?: boolean;
}

interface MenuButtonProps {
  label: string;
  items: MenuItem[];
  align?: 'start' | 'end';
  icon?: LucideIcon;
  className?: string;
  /** 'button' — видимая secondary-кнопка с подписью вместо иконки. */
  trigger?: 'icon' | 'button';
  buttonLabel?: string;
}

const MENU_WIDTH = 224;

/**
 * Меню действий. Список рендерится порталом с фиксированными координатами,
 * чтобы не обрезаться прокручиваемыми таблицами и нижней панелью.
 */
export function MenuButton({
  label,
  items,
  align = 'end',
  icon: Icon = MoreHorizontal,
  className = '',
  trigger = 'icon',
  buttonLabel,
}: MenuButtonProps) {
  const [open, setOpen] = useState(false);
  const [position, setPosition] = useState<{ top: number; left: number } | null>(null);
  const triggerRef = useRef<HTMLButtonElement | null>(null);
  const menuRef = useRef<HTMLDivElement | null>(null);

  const close = () => {
    setOpen(false);
    setPosition(null);
  };

  const openMenu = () => {
    const rect = triggerRef.current?.getBoundingClientRect();
    if (!rect) return;
    const estimatedHeight = Math.min(items.length * 34 + 12, 340);
    const left = align === 'end' ? rect.right - MENU_WIDTH : rect.left;
    const below = rect.bottom + 5;
    const top =
      below + estimatedHeight + 8 > window.innerHeight
        ? Math.max(8, rect.top - estimatedHeight - 5)
        : below;
    setPosition({
      top,
      left: Math.max(8, Math.min(left, window.innerWidth - MENU_WIDTH - 8)),
    });
    setOpen(true);
  };

  useEffect(() => {
    if (!open) return;
    const onPointerDown = (event: MouseEvent | TouchEvent) => {
      const target = event.target as Node;
      if (triggerRef.current?.contains(target)) return;
      if (menuRef.current?.contains(target)) return;
      close();
    };
    const onKeyDown = (event: KeyboardEvent) => {
      if (event.key !== 'Escape') return;
      const target = event.target;
      if (
        target instanceof HTMLElement &&
        (target.tagName === 'INPUT' ||
          target.tagName === 'TEXTAREA' ||
          target.tagName === 'SELECT' ||
          target.isContentEditable)
      ) {
        return;
      }
      close();
    };
    const onScrollOrResize = () => close();
    document.addEventListener('mousedown', onPointerDown);
    document.addEventListener('touchstart', onPointerDown);
    document.addEventListener('keydown', onKeyDown);
    window.addEventListener('scroll', onScrollOrResize, true);
    window.addEventListener('resize', onScrollOrResize);
    return () => {
      document.removeEventListener('mousedown', onPointerDown);
      document.removeEventListener('touchstart', onPointerDown);
      document.removeEventListener('keydown', onKeyDown);
      window.removeEventListener('scroll', onScrollOrResize, true);
      window.removeEventListener('resize', onScrollOrResize);
    };
  }, [open]);

  return (
    <div className={`menu ${trigger === 'button' ? 'menu--button' : ''} ${className}`.trim()}>
      <button
        ref={triggerRef}
        type="button"
        className={
          trigger === 'button'
            ? `btn btn--secondary btn--md ${open ? 'is-active' : ''}`.trim()
            : `icon-btn icon-btn--sm ${open ? 'is-active' : ''}`.trim()
        }
        aria-label={label}
        aria-haspopup="menu"
        aria-expanded={open}
        title={label}
        onClick={() => (open ? close() : openMenu())}
      >
        <Icon size={16} aria-hidden="true" />
        {trigger === 'button' && <span className="btn__label">{buttonLabel ?? label}</span>}
        {trigger === 'button' && <ChevronDown size={14} aria-hidden="true" />}
      </button>
      {open &&
        position &&
        createPortal(
          <div
            ref={menuRef}
            className="menu__list menu__list--fixed"
            role="menu"
            style={{ top: position.top, left: position.left, width: MENU_WIDTH }}
          >
            {items.map((item) => {
              const ItemIcon = item.icon;
              return (
                <button
                  key={item.label}
                  type="button"
                  role="menuitem"
                  className={`menu__item ${item.danger ? 'menu__item--danger' : ''} ${
                    item.separatorBefore ? 'menu__item--sep' : ''
                  }`.trim()}
                  disabled={item.disabled}
                  title={item.disabled ? item.hint : undefined}
                  onClick={() => {
                    if (item.disabled) return;
                    close();
                    item.onSelect();
                  }}
                >
                  {ItemIcon ? <ItemIcon size={15} aria-hidden="true" /> : <span className="menu__spacer" />}
                  <span>{item.label}</span>
                </button>
              );
            })}
          </div>,
          document.body,
        )}
    </div>
  );
}
