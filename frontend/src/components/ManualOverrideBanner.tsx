import { useState } from 'react';
import { Hand } from 'lucide-react';
import { Button } from './ui';
import { useLive, effectiveControl } from '../lib/live';
import { useToast } from './Toasts';
import { api, errorMessage } from '../lib/api';
import { effectiveSourceName } from '../lib/format';
import { notifyDataChanged } from '../lib/refresh';

/**
 * Показывается только при реальном override из station/state.
 * Исчезает сразу после «Завершить переключение», не дожидаясь SSE.
 */
export function ManualOverrideBanner({ compact = false }: { compact?: boolean }) {
  const { now, control, reload, reloadControl } = useLive();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const [hiddenKey, setHiddenKey] = useState<string | null>(null);
  const state = effectiveControl(control, now);
  const override = state?.override ?? null;

  if (!override || !override.active) return null;

  const key = `${override.id || override.kind}:${override.playlistId ?? ''}:${override.createdAt ?? override.expiresAt ?? '1'}`;
  if (hiddenKey === key) return null;

  const name = override.playlistName ?? (state?.effectiveSource?.kind === 'override'
    ? effectiveSourceName(state.effectiveSource)
    : null);
  const backLabel =
    state?.mode === 'auto' ? 'Вернуться к расписанию' : 'Вернуться к плейлисту';

  const clear = async () => {
    setHiddenKey(key);
    setBusy(true);
    try {
      await api.admin.station.clearOverride();
      toast.notify('success', 'Временное переключение завершено');
      reload();
      reloadControl();
      notifyDataChanged();
    } catch (caught) {
      setHiddenKey(null);
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`banner banner--accent ${compact ? 'banner--compact' : ''}`.trim()} role="status">
      <Hand size={16} aria-hidden="true" />
      <div className="banner__body">
        <strong>Временное переключение{name ? `: ${name}` : ''}</strong>
        <p>
          {state?.mode === 'auto'
            ? 'Сейчас играет временный источник вместо расписания.'
            : 'Сейчас играет временный источник вместо выбранного плейлиста.'}
        </p>
      </div>
      <Button variant="secondary" size="sm" onClick={() => void clear()} loading={busy}>
        {backLabel}
      </Button>
    </div>
  );
}
