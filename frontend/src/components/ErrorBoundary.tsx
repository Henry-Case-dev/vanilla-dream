import { Component } from 'react';
import type { ErrorInfo, ReactNode } from 'react';

interface Props {
  children: ReactNode;
}

interface State {
  failed: boolean;
}

/** Резервный экран при непредвиденной ошибке рендера — вместо белого экрана. */
export class ErrorBoundary extends Component<Props, State> {
  state: State = { failed: false };

  static getDerivedStateFromError(): State {
    return { failed: true };
  }

  componentDidCatch(error: Error, info: ErrorInfo): void {
    if (import.meta.env.DEV) {
      console.error('Ошибка интерфейса:', error, info.componentStack);
    }
  }

  render(): ReactNode {
    if (!this.state.failed) return this.props.children;
    return (
      <div className="fatal">
        <h1>Интерфейс не смог продолжить работу</h1>
        <p>Обновите страницу. Если ошибка повторяется — проверьте состояние служб на сервере.</p>
        <button type="button" className="btn btn--primary btn--md" onClick={() => window.location.reload()}>
          <span className="btn__label">Обновить страницу</span>
        </button>
      </div>
    );
  }
}
