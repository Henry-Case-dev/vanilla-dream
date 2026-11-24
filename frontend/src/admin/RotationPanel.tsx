import { useEffect, useState } from 'react';
import { ArrowDown, ArrowUp, Eye, Plus, RefreshCw, RotateCw, Trash2 } from 'lucide-react';
import { Button, Chip, IconButton, Panel, SelectField, Switch } from '../components/ui';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { useToast } from '../components/Toasts';
import { useResource } from '../lib/hooks';
import { api, errorMessage } from '../lib/api';
import { rotationModeLabel } from '../lib/format';
import type { Rotation, RotationMode } from '../lib/types';

/**
 * Редактор ротации: вкладка раздела «Плейлисты».
 * Порядок и параметры элементов принадлежат backend; здесь только редактирование.
 */
export function RotationPanel() {
  const toast = useToast();
  const playlists = useResource(() => api.admin.playlists.list(), []);
  const rotation = useResource(() => api.admin.rotation.get(), []);

  const [draft, setDraft] = useState<Rotation | null>(null);
  const [saving, setSaving] = useState(false);
  const [preview, setPreview] = useState<string | null>(null);
  const [previewBusy, setPreviewBusy] = useState(false);
  const [addPlaylistId, setAddPlaylistId] = useState('');

  useEffect(() => {
    if (rotation.data && draft === null) {
      setDraft({
        mode: rotation.data.mode,
        items: rotation.data.items.map((item) => ({ ...item })),
      });
    }
  }, [rotation.data, draft]);

  const playlistName = (id: string, fallback: string | null): string =>
    playlists.data?.find((playlist) => playlist.id === id)?.name ?? fallback ?? 'Плейлист удалён';

  const updateDraft = (updater: (current: Rotation) => Rotation) => {
    setDraft((current) => (current ? updater(current) : current));
  };

  const move = (index: number, direction: -1 | 1) => {
    updateDraft((current) => {
      const target = index + direction;
      if (target < 0 || target >= current.items.length) return current;
      const items = [...current.items];
      const [item] = items.splice(index, 1);
      items.splice(target, 0, item);
      return { ...current, items };
    });
  };

  const removeItem = (index: number) => {
    updateDraft((current) => ({
      ...current,
      items: current.items.filter((_, itemIndex) => itemIndex !== index),
    }));
  };

  const addItem = () => {
    if (!addPlaylistId) return;
    const name = playlistName(addPlaylistId, null);
    updateDraft((current) => ({
      ...current,
      items: [
        ...current.items,
        {
          id: null,
          playlistId: addPlaylistId,
          playlistName: name,
          enabled: true,
          trackCount: 1,
          position: current.items.length,
        },
      ],
    }));
    setAddPlaylistId('');
  };

  const save = async () => {
    if (!draft) return;
    setSaving(true);
    try {
      await api.admin.rotation.save({
        mode: draft.mode,
        items: draft.items.map((item, index) => ({
          playlist_id: item.playlistId,
          enabled: item.enabled,
          track_count: item.trackCount,
          position: index,
        })),
      });
      toast.notify('success', 'Ротация сохранена');
      const fresh = await api.admin.rotation.get();
      setDraft({ mode: fresh.mode, items: fresh.items });
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setSaving(false);
    }
  };

  const checkPreview = async () => {
    setPreviewBusy(true);
    try {
      const result = await api.admin.rotation.preview();
      setPreview(
        result.playlistName
          ? `Следующим по ротации будет: ${result.playlistName}`
          : 'Ротация пока не может выбрать плейлист',
      );
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setPreviewBusy(false);
    }
  };

  const availablePlaylists = (playlists.data ?? []).filter(
    (playlist) => !draft?.items.some((item) => item.playlistId === playlist.id),
  );

  if (rotation.loading && !rotation.data) {
    return <LoadingState label="Загружаем ротацию…" rows={3} />;
  }

  if (rotation.error && !rotation.data) {
    return <ErrorState message={rotation.error} onRetry={() => void rotation.reload()} />;
  }

  return (
    <Panel className="rotation-panel">
      <div className="panel-head">
        <div>
          <h2 className="panel-title">Ротация плейлистов</h2>
          <p className="panel-subtitle">
            Используется, когда активное правило расписания не задаёт плейлист. Пустые и
            отключённые плейлисты пропускаются движком.
          </p>
        </div>
        <div className="panel-head__actions">
          <Button variant="ghost" size="sm" icon={Eye} onClick={() => void checkPreview()} loading={previewBusy}>
            Что дальше
          </Button>
          <Button variant="ghost" size="sm" icon={RefreshCw} onClick={() => void rotation.reload()}>
            Обновить
          </Button>
          <Button variant="primary" size="sm" icon={RotateCw} onClick={() => void save()} loading={saving} disabled={!draft}>
            Сохранить
          </Button>
        </div>
      </div>

      {preview && <p className="panel-note">{preview}</p>}

      {draft ? (
        <>
          <div className="rotation-mode">
            <SelectField
              label="Режим ротации"
              value={draft.mode}
              onChange={(event) => updateDraft((current) => ({ ...current, mode: event.target.value as RotationMode }))}
              hint={rotationModeLabel(draft.mode)}
            >
              <option value="sequential">По порядку</option>
              <option value="by_count">По количеству треков</option>
              <option value="random">Случайный выбор</option>
            </SelectField>
          </div>

          {draft.items.length === 0 ? (
            <EmptyState
              compact
              icon={RotateCw}
              title="Ротация пуста"
              description="Добавьте плейлисты — движок будет чередовать их в эфире."
            />
          ) : (
            <ul className="rotation-list">
              {draft.items.map((item, index) => (
                <li key={`${item.playlistId}-${index}`} className="rotation-row">
                  <span className="rotation-row__position">{index + 1}</span>
                  <div className="rotation-row__text">
                    <span className="rotation-row__name">{playlistName(item.playlistId, item.playlistName)}</span>
                    <span className="rotation-row__meta">
                      {draft.mode === 'by_count'
                        ? `Берётся треков: ${item.trackCount}`
                        : draft.mode === 'random'
                          ? 'Случайный выбор'
                          : 'Полный проход плейлиста'}
                    </span>
                  </div>
                  {draft.mode === 'by_count' && (
                    <label className="rotation-row__count">
                      <span className="visually-hidden">Количество треков из плейлиста</span>
                      <input
                        type="number"
                        min={1}
                        max={99}
                        className="input input--mini"
                        value={item.trackCount}
                        onChange={(event) => {
                          const value = Math.max(1, Math.min(99, Number(event.target.value) || 1));
                          updateDraft((current) => ({
                            ...current,
                            items: current.items.map((entry, itemIndex) =>
                              itemIndex === index ? { ...entry, trackCount: value } : entry,
                            ),
                          }));
                        }}
                      />
                    </label>
                  )}
                  <Switch
                    checked={item.enabled}
                    label="Включён"
                    onChange={(checked) =>
                      updateDraft((current) => ({
                        ...current,
                        items: current.items.map((entry, itemIndex) =>
                          itemIndex === index ? { ...entry, enabled: checked } : entry,
                        ),
                      }))
                    }
                  />
                  <div className="rotation-row__actions">
                    <IconButton
                      icon={ArrowUp}
                      label="Выше"
                      size="sm"
                      disabled={index === 0}
                      onClick={() => move(index, -1)}
                    />
                    <IconButton
                      icon={ArrowDown}
                      label="Ниже"
                      size="sm"
                      disabled={index === draft.items.length - 1}
                      onClick={() => move(index, 1)}
                    />
                    <IconButton
                      icon={Trash2}
                      label="Убрать из ротации"
                      size="sm"
                      tone="danger"
                      onClick={() => removeItem(index)}
                    />
                  </div>
                </li>
              ))}
            </ul>
          )}

          <div className="rotation-add">
            <SelectField
              label="Добавить плейлист"
              value={addPlaylistId}
              onChange={(event) => setAddPlaylistId(event.target.value)}
              hint={
                availablePlaylists.length === 0
                  ? 'Все плейлисты уже добавлены'
                  : undefined
              }
            >
              <option value="">Выберите плейлист</option>
              {availablePlaylists.map((playlist) => (
                <option key={playlist.id} value={playlist.id}>
                  {playlist.name}
                </option>
              ))}
            </SelectField>
            <Button variant="secondary" icon={Plus} onClick={addItem} disabled={!addPlaylistId}>
              Добавить
            </Button>
          </div>

          <p className="panel-note">
            <Chip tone="neutral">Подсказка</Chip> Порядок элементов определяет очередь обхода.
            Изменения вступают в силу после сохранения и применяются к будущим трекам.
          </p>
        </>
      ) : (
        <EmptyState compact title="Ротация недоступна" description="Сервер не вернул конфигурацию ротации." />
      )}
    </Panel>
  );
}
