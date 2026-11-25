import { useEffect, useState } from 'react';
import { Hand, Radio, Wand2 } from 'lucide-react';
import { Button, SelectField } from './ui';
import { Modal } from './Modal';
import { useToast } from './Toasts';
import { useLive, effectiveControl } from '../lib/live';
import { usePlaylistSettings } from '../lib/playlist-settings';
import { api, errorMessage } from '../lib/api';
import { effectiveSourceName, effectiveSourceWhy } from '../lib/format';
import { notifyDataChanged } from '../lib/refresh';
import type { PlaybackMode } from '../lib/types';

export function TemporarySwitchDialog({
  open,
  onClose,
  defaultPlaylistId,
}: {
  open: boolean;
  onClose: () => void;
  defaultPlaylistId: string;
}) {
  const toast = useToast();
  const { reload, reloadControl } = useLive();
  const settings = usePlaylistSettings();
  const [playlistId, setPlaylistId] = useState(defaultPlaylistId);
  const [scope, setScope] = useState<'until_schedule_change' | 'indefinite'>(
    'until_schedule_change',
  );
  const [busy, setBusy] = useState(false);

  useEffect(() => {
    if (!open) return;
    setPlaylistId(defaultPlaylistId);
    setScope('until_schedule_change');
  }, [open, defaultPlaylistId]);

  const submit = async () => {
    if (!playlistId) return;
    setBusy(true);
    try {
      await api.admin.station.setOverride({ playlistId, scope });
      toast.notify('success', 'Временное переключение включено');
      reload();
      reloadControl();
      notifyDataChanged();
      onClose();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  return (
    <Modal
      open={open}
      onClose={() => (busy ? undefined : onClose())}
      title="Временное переключение"
      description="Эфир временно пойдёт по выбранному плейлисту. Режим и выбранный источник сохранятся."
      footer={
        <>
          <Button variant="ghost" onClick={onClose} disabled={busy}>
            Отмена
          </Button>
          <Button variant="primary" onClick={() => void submit()} loading={busy} disabled={!playlistId}>
            Включить
          </Button>
        </>
      }
    >
      <div className="form-grid">
        <SelectField
          label="Плейлист"
          value={playlistId}
          onChange={(event) => setPlaylistId(event.target.value)}
        >
          <option value="">Выберите плейлист</option>
          {(settings.playlists ?? []).map((playlist) => (
            <option key={playlist.id} value={playlist.id}>
              {playlist.name}
            </option>
          ))}
        </SelectField>
        <div className="source-scope" role="radiogroup" aria-label="Длительность переключения">
          <button
            type="button"
            role="radio"
            aria-checked={scope === 'until_schedule_change'}
            className={`source-scope__option ${scope === 'until_schedule_change' ? 'is-active' : ''}`.trim()}
            onClick={() => setScope('until_schedule_change')}
          >
            До ближайшей смены расписания
          </button>
          <button
            type="button"
            role="radio"
            aria-checked={scope === 'indefinite'}
            className={`source-scope__option ${scope === 'indefinite' ? 'is-active' : ''}`.trim()}
            onClick={() => setScope('indefinite')}
          >
            До отмены
          </button>
        </div>
      </div>
    </Modal>
  );
}

export function SourceControl({ compact = false }: { compact?: boolean }) {
  const toast = useToast();
  const { now, control, reload, reloadControl, controlError } = useLive();
  const settings = usePlaylistSettings();
  const state = effectiveControl(control, now);
  const [busy, setBusy] = useState<PlaybackMode | null>(null);
  const [tempOpen, setTempOpen] = useState(false);

  const targetId =
    state?.mode === 'auto'
      ? state.programPlaylistId ?? ''
      : state?.selectedPlaylistId ?? state?.programPlaylistId ?? '';
  const options = settings.playlists ?? [];
  const pending = state?.pendingSource ?? null;
  const pendingPlaylist = pending
    ? options.find((playlist) => playlist.id === pending.playlistId) ?? null
    : null;
  const effectiveName = effectiveSourceName(state?.effectiveSource);
  const effectiveWhy = effectiveSourceWhy(state?.effectiveSource, state?.mode);

  const setMode = async (mode: PlaybackMode, playlistId?: string) => {
    setBusy(mode);
    try {
      await api.admin.station.setMode(mode, playlistId);
      reload();
      reloadControl();
      notifyDataChanged();
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(null);
    }
  };

  const choosePlaylist = (id: string) => {
    if (!id) return;
    void setMode('playlist', id);
  };

  const disabled = !state || settings.loading || busy !== null;

  return (
    <div className={`source-control ${compact ? 'source-control--compact' : ''}`.trim()}>
      <div className="source-control__row">
        <span className="source-control__label">
          <Radio size={14} aria-hidden="true" /> Источник эфира:
        </span>
        <SelectField
          label="Источник эфира"
          hideLabel
          className="source-control__select"
          value={targetId}
          onChange={(event) => choosePlaylist(event.target.value)}
          disabled={disabled || options.length === 0}
        >
          {!state ? (
            <option value="">Загрузка состояния…</option>
          ) : options.length === 0 ? (
            <option value="">{settings.loading ? 'Загрузка плейлистов…' : 'Плейлисты недоступны'}</option>
          ) : (
            options.map((playlist) => (
              <option key={playlist.id} value={playlist.id}>
                {playlist.name}
              </option>
            ))
          )}
        </SelectField>

        <div className="segmented" role="group" aria-label="Режим источника">
          <button
            type="button"
            className={`segmented__item ${state?.mode === 'playlist' ? 'is-active' : ''}`.trim()}
            aria-pressed={state?.mode === 'playlist'}
            disabled={disabled || !targetId}
            onClick={() => void setMode('playlist', targetId || undefined)}
          >
            Плейлист
          </button>
          <button
            type="button"
            className={`segmented__item ${state?.mode === 'auto' ? 'is-active' : ''}`.trim()}
            aria-pressed={state?.mode === 'auto'}
            disabled={disabled}
            onClick={() => void setMode('auto')}
          >
            Автоматически
          </button>
        </div>

        <Button
          variant="ghost"
          size="sm"
          icon={Hand}
          disabled={!state || options.length === 0}
          onClick={() => setTempOpen(true)}
        >
          Временное переключение
        </Button>
      </div>

      {state?.mode === 'auto' && effectiveName && (
        <p className="source-control__effective">
          Сейчас: <strong>{effectiveName}</strong>
          {effectiveWhy ? ` · ${effectiveWhy}` : ''}
        </p>
      )}
      {pending && (
        <p className="source-control__pending">
          <Wand2 size={13} aria-hidden="true" />
          После текущего трека: {pendingPlaylist?.name ?? 'выбранный плейлист'}
        </p>
      )}
      {state?.notice && <p className="source-control__notice">{state.notice}</p>}
      {controlError && <p className="source-control__error">{controlError}</p>}

      <TemporarySwitchDialog
        open={tempOpen}
        onClose={() => setTempOpen(false)}
        defaultPlaylistId={targetId || options[0]?.id || ''}
      />
    </div>
  );
}
