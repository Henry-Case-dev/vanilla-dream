import { useState } from 'react';
import type { FormEvent } from 'react';
import { Link, Navigate, useLocation, useNavigate } from 'react-router-dom';
import { Logo } from '../components/Logo';
import { Button, TextField } from '../components/ui';
import { useAuth } from '../lib/auth';
import { errorMessage } from '../lib/api';

export function LoginPage() {
  const { state, bootError, login, refresh } = useAuth();
  const navigate = useNavigate();
  const location = useLocation();
  const [password, setPassword] = useState('');
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  const from = (location.state as { from?: string } | null)?.from;
  const target = from && from.startsWith('/admin') ? from : '/admin';

  if (state === 'authenticated') {
    return <Navigate to={target} replace />;
  }

  const submit = async (event: FormEvent<HTMLFormElement>) => {
    event.preventDefault();
    if (!password) {
      setError('Введите пароль администратора');
      return;
    }
    setBusy(true);
    setError(null);
    try {
      await login(password);
      navigate(target, { replace: true });
    } catch (caught) {
      setError(errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="login">
      <div className="login__card">
        <div className="login__brand">
          <Logo />
        </div>
        <h1 className="login__title">Панель управления</h1>
        <p className="login__subtitle">Введите пароль администратора станции.</p>

        {bootError && (
          <div className="login__boot-error" role="status">
            <p>{bootError}</p>
            <Button variant="secondary" size="sm" onClick={() => void refresh()}>
              Проверить снова
            </Button>
          </div>
        )}

        <form className="login__form" onSubmit={(event) => void submit(event)}>
          <TextField
            label="Пароль"
            type="password"
            autoComplete="current-password"
            value={password}
            onChange={(event) => setPassword(event.target.value)}
            placeholder="Пароль администратора"
            error={error}
            autoFocus
          />
          <Button type="submit" variant="primary" block loading={busy}>
            Войти
          </Button>
        </form>

        <p className="login__hint">
          Пароль задаётся на сервере при установке станции. Если он утерян — восстановление
          выполняется через CLI на сервере.
        </p>

        <Link to="/" className="login__back">
          ← Публичный плеер
        </Link>
      </div>
      <p className="login__foot">
        Vanilla Dream Radio · <span>круглосуточный эфир</span>
      </p>
    </div>
  );
}
