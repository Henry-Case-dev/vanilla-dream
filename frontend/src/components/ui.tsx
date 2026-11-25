import { useId } from 'react';
import type {
  ButtonHTMLAttributes,
  InputHTMLAttributes,
  ReactNode,
  SelectHTMLAttributes,
  TextareaHTMLAttributes,
} from 'react';
import { Loader2 } from 'lucide-react';
import type { LucideIcon } from 'lucide-react';

/* ------------------------------------------------------------------ */
/* Кнопки                                                              */
/* ------------------------------------------------------------------ */

export type ButtonVariant = 'primary' | 'secondary' | 'ghost' | 'danger' | 'success';

interface ButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  variant?: ButtonVariant;
  size?: 'sm' | 'md' | 'lg';
  icon?: LucideIcon;
  iconRight?: LucideIcon;
  loading?: boolean;
  block?: boolean;
}

export function Button({
  variant = 'secondary',
  size = 'md',
  icon: Icon,
  iconRight: IconRight,
  loading = false,
  block = false,
  className = '',
  children,
  disabled,
  type = 'button',
  ...rest
}: ButtonProps) {
  return (
    <button
      type={type}
      className={`btn btn--${variant} btn--${size} ${block ? 'btn--block' : ''} ${className}`.trim()}
      disabled={disabled || loading}
      aria-busy={loading || undefined}
      {...rest}
    >
      {loading ? (
        <Loader2 size={16} className="spin" aria-hidden="true" />
      ) : (
        Icon && <Icon size={size === 'sm' ? 14 : 16} aria-hidden="true" />
      )}
      {children !== undefined && children !== null && children !== '' && (
        <span className="btn__label">{children}</span>
      )}
      {IconRight && <IconRight size={size === 'sm' ? 14 : 16} aria-hidden="true" />}
    </button>
  );
}

interface IconButtonProps extends ButtonHTMLAttributes<HTMLButtonElement> {
  label: string;
  icon: LucideIcon;
  size?: 'sm' | 'md' | 'lg';
  tone?: 'default' | 'accent' | 'danger';
  active?: boolean;
  loading?: boolean;
}

export function IconButton({
  label,
  icon: Icon,
  size = 'md',
  tone = 'default',
  active = false,
  loading = false,
  className = '',
  type = 'button',
  ...rest
}: IconButtonProps) {
  return (
    <button
      type={type}
      className={`icon-btn icon-btn--${size} icon-btn--${tone} ${active ? 'is-active' : ''} ${className}`.trim()}
      aria-label={label}
      title={label}
      aria-pressed={active || undefined}
      aria-busy={loading || undefined}
      {...rest}
    >
      <Icon
        size={size === 'lg' ? 20 : size === 'sm' ? 14 : 17}
        className={loading ? 'spin' : undefined}
        aria-hidden="true"
      />
    </button>
  );
}

/* ------------------------------------------------------------------ */
/* Поля ввода                                                          */
/* ------------------------------------------------------------------ */

interface FieldExtras {
  label: string;
  hint?: ReactNode;
  error?: string | null;
  hideLabel?: boolean;
}

function FieldShell({
  id,
  label,
  hint,
  error,
  hideLabel,
  children,
}: FieldExtras & { id: string; children: ReactNode }) {
  return (
    <div className={`field ${error ? 'field--error' : ''}`}>
      <label className={hideLabel ? 'visually-hidden' : 'field__label'} htmlFor={id}>
        {label}
      </label>
      {children}
      {error ? (
        <span className="field__error" role="alert">
          {error}
        </span>
      ) : hint ? (
        <span className="field__hint">{hint}</span>
      ) : null}
    </div>
  );
}

type TextFieldProps = FieldExtras & InputHTMLAttributes<HTMLInputElement>;

export function TextField({ label, hint, error, hideLabel, className = '', id, ...rest }: TextFieldProps) {
  const generated = useId();
  const fieldId = id ?? generated;
  return (
    <FieldShell id={fieldId} label={label} hint={hint} error={error} hideLabel={hideLabel}>
      <input id={fieldId} className={`input ${error ? 'input--error' : ''} ${className}`.trim()} {...rest} />
    </FieldShell>
  );
}

type SelectFieldProps = FieldExtras & SelectHTMLAttributes<HTMLSelectElement> & { children: ReactNode };

export function SelectField({
  label,
  hint,
  error,
  hideLabel,
  className = '',
  id,
  children,
  ...rest
}: SelectFieldProps) {
  const generated = useId();
  const fieldId = id ?? generated;
  return (
    <FieldShell id={fieldId} label={label} hint={hint} error={error} hideLabel={hideLabel}>
      <select id={fieldId} className={`select ${error ? 'input--error' : ''} ${className}`.trim()} {...rest}>
        {children}
      </select>
    </FieldShell>
  );
}

type TextAreaFieldProps = FieldExtras & TextareaHTMLAttributes<HTMLTextAreaElement>;

export function TextAreaField({
  label,
  hint,
  error,
  hideLabel,
  className = '',
  id,
  ...rest
}: TextAreaFieldProps) {
  const generated = useId();
  const fieldId = id ?? generated;
  return (
    <FieldShell id={fieldId} label={label} hint={hint} error={error} hideLabel={hideLabel}>
      <textarea id={fieldId} className={`textarea ${error ? 'input--error' : ''} ${className}`.trim()} {...rest} />
    </FieldShell>
  );
}

interface SwitchProps {
  checked: boolean;
  onChange: (checked: boolean) => void;
  label: string;
  description?: string;
  disabled?: boolean;
  ariaLabel?: string;
}

export function Switch({ checked, onChange, label, description, disabled = false, ariaLabel }: SwitchProps) {
  return (
    <label className={`switch ${disabled ? 'is-disabled' : ''}`.trim()}>
      <input
        type="checkbox"
        className="switch__input"
        checked={checked}
        disabled={disabled}
        aria-label={label ? undefined : ariaLabel}
        onChange={(event) => onChange(event.target.checked)}
      />
      <span className="switch__track" aria-hidden="true">
        <span className="switch__thumb" />
      </span>
      {(label || description) && (
        <span className="switch__text">
          {label && <span className="switch__label">{label}</span>}
          {description && <span className="switch__desc">{description}</span>}
        </span>
      )}
    </label>
  );
}

/* ------------------------------------------------------------------ */
/* Индикаторы                                                          */
/* ------------------------------------------------------------------ */

export function Spinner({ size = 18, className = '' }: { size?: number; className?: string }) {
  return <Loader2 size={size} className={`spin ${className}`.trim()} aria-hidden="true" />;
}

interface ProgressBarProps {
  /** 0..1 */
  value: number;
  className?: string;
  tone?: 'accent' | 'success';
  label?: string;
}

export function ProgressBar({ value, className = '', tone = 'accent', label }: ProgressBarProps) {
  const clamped = Math.min(1, Math.max(0, Number.isFinite(value) ? value : 0));
  return (
    <div
      className={`progress progress--${tone} ${className}`.trim()}
      role="progressbar"
      aria-valuemin={0}
      aria-valuemax={100}
      aria-valuenow={Math.round(clamped * 100)}
      aria-label={label}
    >
      <span className="progress__fill" style={{ width: `${clamped * 100}%` }} />
    </div>
  );
}

export function Equalizer({ active = true }: { active?: boolean }) {
  return (
    <span className={`eq ${active ? 'is-active' : ''}`.trim()} aria-hidden="true">
      <span className="eq__bar" />
      <span className="eq__bar" />
      <span className="eq__bar" />
      <span className="eq__bar" />
    </span>
  );
}

/* ------------------------------------------------------------------ */
/* Компоновка                                                          */
/* ------------------------------------------------------------------ */

export function Panel({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <section className={`panel ${className}`.trim()}>{children}</section>;
}

export function PageHeader({
  title,
  subtitle,
  actions,
  eyebrow,
}: {
  title: string;
  subtitle?: ReactNode;
  actions?: ReactNode;
  eyebrow?: string;
}) {
  return (
    <header className="page-header">
      <div className="page-header__text">
        {eyebrow && <span className="eyebrow">{eyebrow}</span>}
        <h1 className="page-header__title">{title}</h1>
        {subtitle && <p className="page-header__subtitle">{subtitle}</p>}
      </div>
      {actions && <div className="page-header__actions">{actions}</div>}
    </header>
  );
}

export function Chip({
  children,
  tone = 'neutral',
  className = '',
  title,
}: {
  children: ReactNode;
  tone?: 'neutral' | 'accent' | 'success' | 'warning' | 'danger';
  className?: string;
  title?: string;
}) {
  return (
    <span className={`chip chip--${tone} ${className}`.trim()} title={title}>
      {children}
    </span>
  );
}

export function MetaRow({ children, className = '' }: { children: ReactNode; className?: string }) {
  return <div className={`meta-row ${className}`.trim()}>{children}</div>;
}
