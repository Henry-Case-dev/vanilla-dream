import { useState } from 'react';
import { Hand } from 'lucide-react';
import { Button } from './ui';
import { useLive } from '../lib/live';
import { useToast } from './Toasts';
import { api, errorMessage } from '../lib/api';
import { sourceDisplayName } from '../lib/format';
import { notifyDataChanged } from '../lib/refresh';

/**
 * Ручной выбор всегда виден в интерфейсе, пока активен override.
 * Возврат к автоматическому режиму — DELETE /api/admin/station/override.
 */
export function ManualOverrideBanner({ compact = false }: { compact?: boolean }) {
  const { now, reload } = useLive();
  const toast = useToast();
  const [busy, setBusy] = useState(false);
  const source = now?.source;

  if (!source || source.kind !== 'override') return null;

  const name = sourceDisplayName(source);

  const clear = async () => {
    setBusy(true);
    try {
      await api.admin.station.clearOverride();
      toast.notify('success', 'Снова работает автоматический выбор источника');
      reload();
      notifyDataChanged();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className={`banner banner--accent ${compact ? 'banner--compact' : ''}`.trim()} role="status">
      <Hand size={16} aria-hidden="true" />
      <div className="banner__body">
        <strong>Ручной выбор{name ? `: ${name}` : ''}</strong>
        <p>Эфир идёт по ручному переключению, а не по расписанию или ротации.</p>
      </div>
      <Button variant="secondary" size="sm" onClick={() => void clear()} loading={busy}>
        Вернуться к автоматическому
      </Button>
    </div>
  );
}
