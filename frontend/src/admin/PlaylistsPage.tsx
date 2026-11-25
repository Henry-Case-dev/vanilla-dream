import { useState } from 'react';
import { Link, useNavigate, useSearchParams } from 'react-router-dom';
import { ListMusic, Plus, RefreshCw } from 'lucide-react';
import { Button, Chip, IconButton, PageHeader, Panel, TextAreaField, TextField } from '../components/ui';
import { Cover } from '../components/Cover';
import { Modal } from '../components/Modal';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { useToast } from '../components/Toasts';
import { useResource } from '../lib/hooks';
import { api, errorMessage } from '../lib/api';
import { formatCount, formatLongDuration, pluralRu, tracksLabel, repeatLabel } from '../lib/format';
import { RotationPanel } from './RotationPanel';
import { notifyDataChanged } from '../lib/refresh';

export function PlaylistsPage() {
  const navigate = useNavigate();
  const toast = useToast();
  const [params, setParams] = useSearchParams();
  const tab = params.get('tab') === 'rotation' ? 'rotation' : 'playlists';

  const playlists = useResource(() => api.admin.playlists.list(), []);
  const [createOpen, setCreateOpen] = useState(false);
  const [name, setName] = useState('');
  const [description, setDescription] = useState('');
  const [busy, setBusy] = useState(false);

  const openCreate = () => {
    setName('');
    setDescription('');
    setCreateOpen(true);
  };

  const create = async () => {
    if (!name.trim()) {
      toast.notify('error', 'Введите название плейлиста');
      return;
    }
    setBusy(true);
    try {
      const playlist = await api.admin.playlists.create({
        name: name.trim(),
        description: description.trim() || undefined,
      });
      toast.notify('success', 'Плейлист создан');
      setCreateOpen(false);
      notifyDataChanged();
      navigate(`/admin/playlists/${playlist.id}`);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBusy(false);
    }
  };

  const setTab = (next: 'playlists' | 'rotation') => {
    setParams(next === 'rotation' ? { tab: 'rotation' } : {});
  };

  const items = playlists.data ?? [];

  return (
    <div className="page page--playlists">
      <PageHeader
        title="Плейлисты"
        subtitle={
          tab === 'playlists'
            ? items.length > 0
              ? `${formatCount(items.length)} ${pluralRu(items.length, [
                  'плейлист',
                  'плейлиста',
                  'плейлистов',
                ])} в медиатеке`
              : 'Наборы музыки для эфира'
            : 'Автоматическое чередование плейлистов'
        }
        actions={
          tab === 'playlists' ? (
            <>
              <IconButton
                icon={RefreshCw}
                label="Обновить список"
                title="Обновить список"
                onClick={() => void playlists.reload()}
              />
              <Button variant="primary" icon={Plus} onClick={openCreate}>
                Создать плейлист
              </Button>
            </>
          ) : undefined
        }
      />

      <div className="tabs" role="tablist" aria-label="Плейлисты и ротация">
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'playlists'}
          className={`tab ${tab === 'playlists' ? 'is-active' : ''}`.trim()}
          onClick={() => setTab('playlists')}
        >
          Плейлисты
        </button>
        <button
          type="button"
          role="tab"
          aria-selected={tab === 'rotation'}
          className={`tab ${tab === 'rotation' ? 'is-active' : ''}`.trim()}
          onClick={() => setTab('rotation')}
        >
          Ротация
        </button>
      </div>

      {tab === 'rotation' ? (
        <RotationPanel />
      ) : playlists.loading && !playlists.data ? (
        <LoadingState label="Загружаем плейлисты…" rows={4} />
      ) : playlists.error && !playlists.data ? (
        <ErrorState message={playlists.error} onRetry={() => void playlists.reload()} />
      ) : items.length === 0 ? (
        <Panel>
          <EmptyState
            icon={ListMusic}
            title="Плейлистов пока нет"
            description="Создайте первый плейлист, добавьте треки и запустите его в эфир."
            action={
              <Button variant="primary" size="sm" icon={Plus} onClick={openCreate}>
                Создать плейлист
              </Button>
            }
          />
        </Panel>
      ) : (
        <div className="playlist-grid">
          {items.map((playlist) => (
            <Link key={playlist.id} to={`/admin/playlists/${playlist.id}`} className="playlist-card">
              <div className="playlist-card__cover">
                <Cover src={playlist.coverUrl} alt={`Обложка плейлиста ${playlist.name}`} size="fill" rounded="md" />
              </div>
              <div className="playlist-card__body">
                <div className="playlist-card__title-row">
                  <h3 className="playlist-card__name">{playlist.name}</h3>
                  {playlist.system && (
                    <span className="playlist-system-label">Системный плейлист</span>
                  )}
                </div>
                <p className="playlist-card__desc">{playlist.description || 'Без описания'}</p>
                <div className="playlist-card__meta">
                  <span>{tracksLabel(playlist.trackCount)}</span>
                  <span>{formatLongDuration(playlist.duration)}</span>
                </div>
                {(playlist.shuffle || playlist.repeatMode !== 'none') && (
                  <div className="playlist-card__chips">
                    {playlist.shuffle && <Chip tone="accent">Перемешивание</Chip>}
                    {playlist.repeatMode !== 'none' && (
                      <Chip tone="neutral">{repeatLabel(playlist.repeatMode)}</Chip>
                    )}
                  </div>
                )}
              </div>
            </Link>
          ))}
        </div>
      )}

      <Modal
        open={createOpen}
        onClose={() => (busy ? undefined : setCreateOpen(false))}
        title="Новый плейлист"
        description="Название и описание можно изменить позже."
        footer={
          <>
            <Button variant="ghost" onClick={() => setCreateOpen(false)} disabled={busy}>
              Отмена
            </Button>
            <Button variant="primary" onClick={() => void create()} loading={busy}>
              Создать
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <TextField
            label="Название"
            value={name}
            onChange={(event) => setName(event.target.value)}
            placeholder="Например: Ночной сиреневый"
            autoFocus
          />
          <TextAreaField
            label="Описание"
            rows={3}
            value={description}
            onChange={(event) => setDescription(event.target.value)}
            placeholder="Необязательно"
          />
        </div>
      </Modal>
    </div>
  );
}
