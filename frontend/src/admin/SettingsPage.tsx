import { useEffect, useMemo, useState } from 'react';
import {
  AlertTriangle,
  CheckCircle2,
  Database,
  HardDrive,
  KeyRound,
  LogOut,
  Radio,
  RefreshCw,
  Save,
  Server,
  Settings2,
  XCircle,
} from 'lucide-react';
import {
  Button,
  Chip,
  IconButton,
  PageHeader,
  Panel,
  ProgressBar,
  SelectField,
  Spinner,
  Switch,
  TextAreaField,
  TextField,
} from '../components/ui';
import { ConfirmDialog, Modal } from '../components/Modal';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { useToast } from '../components/Toasts';
import { useLive } from '../lib/live';
import { useResource } from '../lib/hooks';
import { useAuth } from '../lib/auth';
import { api, errorMessage } from '../lib/api';
import {
  formatBytes,
  formatCount,
  formatDateTime,
  isValidTimeZone,
  timeZoneOptions,
  transitionLabel,
} from '../lib/format';

/**
 * Допустимые битрейты backend (`SUPPORTED_BITRATES` в `backend/app/config.py`).
 * Список должен совпадать с сервером: иначе сохранение вернёт 422.
 */
const BITRATES = [96, 112, 128, 160, 192, 224, 256, 320];

interface BroadcastForm {
  name: string;
  description: string;
  genre: string;
  bitrate: number;
  defaultPlaylistId: string;
  transition: string;
}

interface ScheduleForm {
  timezone: string;
  enabled: boolean;
  transition: string;
}

const serviceStateLabel: Record<string, string> = {
  active: 'Работает',
  inactive: 'Остановлена',
  failed: 'Ошибка',
  unknown: 'Неизвестно',
};

const serviceStateTone: Record<string, string> = {
  active: 'dot--live',
  inactive: 'dot--idle',
  failed: 'dot--error',
  unknown: 'dot--idle',
};

export function SettingsPage() {
  const toast = useToast();
  const { now, reload: reloadLive } = useLive();
  const { logout } = useAuth();

  const settings = useResource(() => api.admin.settings.get(), []);
  const storage = useResource(() => api.admin.storage(), [], { pollMs: 60000 });
  const services = useResource(() => api.admin.services(), [], { pollMs: 30000 });
  const playlists = useResource(() => api.admin.playlists.list(), []);

  const [broadcast, setBroadcast] = useState<BroadcastForm | null>(null);
  const [schedule, setSchedule] = useState<ScheduleForm | null>(null);
  const [broadcastBusy, setBroadcastBusy] = useState(false);
  const [scheduleBusy, setScheduleBusy] = useState(false);
  const [bitrateConfirm, setBitrateConfirm] = useState(false);
  const [passwordOpen, setPasswordOpen] = useState(false);
  const [passwordForm, setPasswordForm] = useState({ current: '', next: '', confirm: '' });
  const [passwordBusy, setPasswordBusy] = useState(false);
  const [passwordError, setPasswordError] = useState<string | null>(null);

  useEffect(() => {
    if (settings.data && !broadcast) {
      setBroadcast({
        name: settings.data.stationName,
        description: settings.data.stationDescription,
        genre: settings.data.stationGenre,
        bitrate: settings.data.bitrate,
        defaultPlaylistId: settings.data.defaultPlaylistId,
        transition: settings.data.defaultTransition,
      });
    }
  }, [settings.data, broadcast]);

  useEffect(() => {
    if (settings.data && !schedule) {
      setSchedule({
        timezone: settings.data.timezone,
        enabled: settings.data.scheduleEnabled,
        transition: settings.data.defaultTransition,
      });
    }
  }, [settings.data, schedule]);

  const zones = useMemo(() => timeZoneOptions(), []);

  const persistBroadcast = async () => {
    if (!broadcast) return;
    if (!broadcast.name.trim()) {
      toast.notify('error', 'Название станции не может быть пустым');
      return;
    }
    setBroadcastBusy(true);
    try {
      const saved = await api.admin.settings.save({
        station_name: broadcast.name.trim(),
        station_description: broadcast.description.trim(),
        station_genre: broadcast.genre.trim(),
        bitrate: broadcast.bitrate,
        default_playlist_id: broadcast.defaultPlaylistId,
        default_transition: broadcast.transition,
      });
      setBroadcast({
        name: saved.stationName,
        description: saved.stationDescription,
        genre: saved.stationGenre,
        bitrate: saved.bitrate,
        defaultPlaylistId: saved.defaultPlaylistId,
        transition: saved.defaultTransition,
      });
      setSchedule((current) =>
        current ? { ...current, transition: saved.defaultTransition } : current,
      );
      toast.notify('success', 'Настройки вещания сохранены');
      reloadLive();
      void settings.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setBroadcastBusy(false);
    }
  };

  const saveBroadcast = () => {
    if (!broadcast || !settings.data) return;
    if (broadcast.bitrate !== settings.data.bitrate) {
      setBitrateConfirm(true);
      return;
    }
    void persistBroadcast();
  };

  const saveSchedule = async () => {
    if (!schedule) return;
    if (!isValidTimeZone(schedule.timezone)) {
      toast.notify('error', 'Неизвестный часовой пояс. Например: Asia/Magadan или Europe/Moscow');
      return;
    }
    setScheduleBusy(true);
    try {
      const saved = await api.admin.settings.save({
        timezone: schedule.timezone,
        schedule_enabled: schedule.enabled,
        default_transition: schedule.transition,
      });
      setSchedule({
        timezone: saved.timezone,
        enabled: saved.scheduleEnabled,
        transition: saved.defaultTransition,
      });
      setBroadcast((current) =>
        current ? { ...current, transition: saved.defaultTransition } : current,
      );
      toast.notify('success', 'Настройки расписания сохранены');
      reloadLive();
      void settings.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setScheduleBusy(false);
    }
  };

  const changePassword = async () => {
    setPasswordError(null);
    if (!passwordForm.current) {
      setPasswordError('Введите текущий пароль');
      return;
    }
    if (passwordForm.next.length < 8) {
      setPasswordError('Новый пароль должен содержать минимум 8 символов');
      return;
    }
    if (passwordForm.next !== passwordForm.confirm) {
      setPasswordError('Новый пароль и подтверждение не совпадают');
      return;
    }
    setPasswordBusy(true);
    try {
      await api.auth.changePassword(passwordForm.current, passwordForm.next);
      toast.notify('success', 'Пароль изменён');
      setPasswordOpen(false);
      setPasswordForm({ current: '', next: '', confirm: '' });
    } catch (caught) {
      setPasswordError(errorMessage(caught));
    } finally {
      setPasswordBusy(false);
    }
  };

  const storageUsed = storage.data?.libraryBytes ?? 0;
  const free = storage.data?.freeBytes ?? null;
  const total = storage.data?.totalBytes ?? null;
  const diskUsed = total && free !== null ? Math.max(0, total - free) : null;
  const diskProgress = total && diskUsed !== null ? diskUsed / total : null;

  if (settings.loading && !settings.data) {
    return (
      <div className="page">
        <PageHeader title="Настройки" subtitle="Вещание, расписание, хранилище и службы" />
        <LoadingState label="Загружаем настройки…" rows={4} />
      </div>
    );
  }

  if (settings.error && !settings.data) {
    return (
      <div className="page">
        <PageHeader title="Настройки" subtitle="Вещание, расписание, хранилище и службы" />
        <ErrorState message={settings.error} onRetry={() => void settings.reload()} />
      </div>
    );
  }

  return (
    <div className="page page--settings">
      <PageHeader
        title="Настройки"
        subtitle="Вещание, расписание, хранилище и службы"
        actions={
          <Button
            variant="ghost"
            size="sm"
            icon={RefreshCw}
            onClick={() => {
              void settings.reload();
              void storage.reload();
              void services.reload();
              void playlists.reload();
            }}
          >
            Обновить
          </Button>
        }
      />

      <div className="settings-grid">
        <Panel className="settings-panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">
                <Radio size={16} aria-hidden="true" /> Вещание
              </h2>
              <p className="panel-subtitle">Название, описание и параметры эфира</p>
            </div>
            <Chip tone="neutral">Движок: {now?.station.bitrate ?? settings.data?.bitrate ?? '—'} кбит/с</Chip>
          </div>

          {broadcast ? (
            <div className="form-grid">
              <TextField
                label="Название станции"
                value={broadcast.name}
                onChange={(event) => setBroadcast({ ...broadcast, name: event.target.value })}
              />
              <TextAreaField
                label="Описание"
                rows={2}
                value={broadcast.description}
                onChange={(event) => setBroadcast({ ...broadcast, description: event.target.value })}
                hint="Используется в публичном плеере и метаданных Icecast"
              />
              <div className="form-grid form-grid--2">
                <TextField
                  label="Жанр"
                  value={broadcast.genre}
                  onChange={(event) => setBroadcast({ ...broadcast, genre: event.target.value })}
                />
                <SelectField
                  label="Битрейт"
                  value={String(broadcast.bitrate)}
                  onChange={(event) => setBroadcast({ ...broadcast, bitrate: Number(event.target.value) })}
                  hint="Изменение перезапускает движок"
                >
                  {BITRATES.map((bitrate) => (
                    <option key={bitrate} value={bitrate}>
                      {bitrate} кбит/с
                    </option>
                  ))}
                  {!BITRATES.includes(broadcast.bitrate) && (
                    <option value={broadcast.bitrate}>{broadcast.bitrate} кбит/с (текущий)</option>
                  )}
                </SelectField>
              </div>
              <div className="form-grid form-grid--2">
                <SelectField
                  label="Плейлист по умолчанию"
                  value={broadcast.defaultPlaylistId}
                  onChange={(event) => setBroadcast({ ...broadcast, defaultPlaylistId: event.target.value })}
                  hint="Если ротация и расписание не задают источник"
                >
                  <option value="">Вся медиатека</option>
                  {broadcast.defaultPlaylistId &&
                    playlists.data &&
                    !playlists.data.some((playlist) => playlist.id === broadcast.defaultPlaylistId) && (
                      <option value={broadcast.defaultPlaylistId}>Плейлист недоступен</option>
                    )}
                  {(playlists.data ?? []).map((playlist) => (
                    <option key={playlist.id} value={playlist.id}>
                      {playlist.name}
                    </option>
                  ))}
                </SelectField>
                <SelectField
                  label="Переход по умолчанию"
                  value={broadcast.transition}
                  onChange={(event) => setBroadcast({ ...broadcast, transition: event.target.value })}
                >
                  <option value="after_track">После трека</option>
                  <option value="immediate">Немедленно</option>
                  {!['after_track', 'immediate'].includes(broadcast.transition) && (
                    <option value={broadcast.transition}>{transitionLabel(broadcast.transition)}</option>
                  )}
                </SelectField>
              </div>

              <div className="settings-actions">
                <Button variant="primary" icon={Save} onClick={saveBroadcast} loading={broadcastBusy}>
                  Сохранить вещание
                </Button>
                {now?.station.bitrate ? (
                  <span className="muted">Фактический битрейт эфира: {now.station.bitrate} кбит/с</span>
                ) : null}
              </div>
            </div>
          ) : (
            <EmptyState compact title="Настройки недоступны" description="Сервер не вернул параметры вещания." />
          )}
        </Panel>

        <Panel className="settings-panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">
                <Settings2 size={16} aria-hidden="true" /> Расписание
              </h2>
              <p className="panel-subtitle">Часовой пояс станции и режим переходов</p>
            </div>
          </div>

          {schedule ? (
            <div className="form-grid">
              <TextField
                label="Часовой пояс станции"
                value={schedule.timezone}
                list="timezone-options"
                onChange={(event) => setSchedule({ ...schedule, timezone: event.target.value })}
                hint="Формат IANA, например Asia/Magadan"
              />
              <datalist id="timezone-options">
                {zones.map((zone) => (
                  <option key={zone} value={zone} />
                ))}
              </datalist>
              <div className="settings-inline">
                <Switch
                  checked={schedule.enabled}
                  onChange={(checked) => setSchedule({ ...schedule, enabled: checked })}
                  label="Расписание включено"
                  description="Если выключено, применяется ротация или плейлист по умолчанию"
                />
              </div>
              <SelectField
                label="Переход по умолчанию"
                value={schedule.transition}
                onChange={(event) => setSchedule({ ...schedule, transition: event.target.value })}
              >
                <option value="after_track">После трека</option>
                <option value="immediate">Немедленно</option>
                {!['after_track', 'immediate'].includes(schedule.transition) && (
                  <option value={schedule.transition}>{transitionLabel(schedule.transition)}</option>
                )}
              </SelectField>
              <div className="settings-actions">
                <Button variant="primary" icon={Save} onClick={() => void saveSchedule()} loading={scheduleBusy}>
                  Сохранить расписание
                </Button>
              </div>
            </div>
          ) : (
            <EmptyState compact title="Настройки недоступны" description="Сервер не вернул параметры расписания." />
          )}
        </Panel>

        <Panel className="settings-panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">
                <HardDrive size={16} aria-hidden="true" /> Хранилище
              </h2>
              <p className="panel-subtitle">Медиатека и свободное место на сервере</p>
            </div>
          </div>

          {storage.loading && !storage.data ? (
            <LoadingState label="Считаем хранилище…" rows={2} />
          ) : storage.error && !storage.data ? (
            <ErrorState message={storage.error} onRetry={() => void storage.reload()} />
          ) : storage.data ? (
            <div className="storage">
              <div className="storage__stats">
                <div className="storage__stat">
                  <span className="storage__label">Треков</span>
                  <span className="storage__value">{formatCount(storage.data.tracks)}</span>
                </div>
                <div className="storage__stat">
                  <span className="storage__label">Занято музыкой</span>
                  <span className="storage__value">{formatBytes(storageUsed)}</span>
                </div>
                <div className="storage__stat">
                  <span className="storage__label">Свободно на диске</span>
                  <span className="storage__value">{formatBytes(free)}</span>
                </div>
              </div>
              {diskProgress !== null && (
                <div className="storage__disk">
                  <ProgressBar value={diskProgress} label="Занято дискового пространства" />
                  <span className="muted">
                    {formatBytes(diskUsed)} из {formatBytes(total)}
                  </span>
                </div>
              )}
              {storage.data.freeBytes !== null && storage.data.freeBytes < 3 * 1024 * 1024 * 1024 && (
                <div className="banner banner--warning" role="status">
                  <AlertTriangle size={16} aria-hidden="true" />
                  <div>
                    <strong>Мало свободного места</strong>
                    <p>Загрузка может быть отклонена сервером при остатке менее 3 ГБ.</p>
                  </div>
                </div>
              )}
              {settings.data?.maxLibraryGb ? (
                <p className="panel-note">
                  Лимит библиотеки: {settings.data.maxLibraryGb} ГБ · лимит файла:{' '}
                  {settings.data.maxUploadMb} МБ
                </p>
              ) : (
                <p className="panel-note">Лимит файла: {settings.data?.maxUploadMb ?? '—'} МБ</p>
              )}
            </div>
          ) : null}
        </Panel>

        <Panel className="settings-panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">
                <Server size={16} aria-hidden="true" /> Службы
              </h2>
              <p className="panel-subtitle">Icecast, Liquidsoap и backend</p>
            </div>
            <IconButton icon={RefreshCw} label="Обновить службы" size="sm" onClick={() => void services.reload()} />
          </div>

          {services.loading && !services.data ? (
            <LoadingState label="Проверяем службы…" rows={3} />
          ) : services.error && !services.data ? (
            <ErrorState message={services.error} onRetry={() => void services.reload()} />
          ) : services.data && services.data.length > 0 ? (
            <ul className="service-list">
              {services.data.map((service) => (
                <li key={service.id} className="service-row">
                  <span className={`dot ${serviceStateTone[service.state]}`} aria-hidden="true" />
                  <span className="service-row__name">{service.name}</span>
                  <span className="service-row__state">
                    {service.state === 'active' ? (
                      <CheckCircle2 size={14} aria-hidden="true" />
                    ) : service.state === 'failed' ? (
                      <XCircle size={14} aria-hidden="true" />
                    ) : null}
                    {serviceStateLabel[service.state]}
                  </span>
                  <span className="service-row__time">
                    {service.lastStart ? `Запуск: ${formatDateTime(service.lastStart, settings.data?.timezone)}` : '—'}
                  </span>
                  {service.lastError && <span className="service-row__error">{service.lastError}</span>}
                </li>
              ))}
            </ul>
          ) : (
            <EmptyState compact title="Нет данных о службах" description="Сервер не вернул список служб." />
          )}

          <div className="service-engine">
            <span className="muted">Движок</span>
            {settings.data?.engineLastStart ? (
              <span>Последний запуск: {formatDateTime(settings.data.engineLastStart, settings.data.timezone)}</span>
            ) : (
              <span className="muted">Запусков ещё не было</span>
            )}
            {settings.data?.engineLastError && (
              <span className="service-engine__error">
                <AlertTriangle size={14} aria-hidden="true" /> {settings.data.engineLastError}
              </span>
            )}
          </div>
        </Panel>

        <Panel className="settings-panel">
          <div className="panel-head">
            <div>
              <h2 className="panel-title">
                <KeyRound size={16} aria-hidden="true" /> Доступ
              </h2>
              <p className="panel-subtitle">Пароль администратора и сессия</p>
            </div>
          </div>
          <p className="panel-note">
            После смены пароля текущая сессия остаётся активной. Другие сессии продолжают действовать
            до истечения срока.
          </p>
          <Button variant="secondary" icon={KeyRound} onClick={() => setPasswordOpen(true)}>
            Сменить пароль
          </Button>
          <Button variant="ghost" icon={LogOut} onClick={() => void logout()}>
            Выйти из панели
          </Button>
          <div className="settings-db">
            <Database size={14} aria-hidden="true" />
            <span>Состояние хранится в SQLite станции; резервные копии настраиваются на сервере.</span>
          </div>
        </Panel>
      </div>

      <ConfirmDialog
        open={bitrateConfirm}
        title="Изменить битрейт эфира?"
        description={
          broadcast && settings.data ? (
            <>
              Битрейт изменится с {settings.data.bitrate} на {broadcast.bitrate} кбит/с. Это требует
              перезапуска движка: эфир прервётся на несколько секунд, слушатели переподключатся.
              Остальные настройки вещания сохранятся вместе с битрейтом.
            </>
          ) : null
        }
        confirmLabel="Изменить и перезапустить"
        danger
        busy={broadcastBusy}
        onConfirm={async () => {
          await persistBroadcast();
          setBitrateConfirm(false);
        }}
        onCancel={() => setBitrateConfirm(false)}
      />

      <Modal
        open={passwordOpen}
        onClose={() => (passwordBusy ? undefined : setPasswordOpen(false))}
        title="Смена пароля"
        description="Минимум 8 символов. Пароль хранится только в виде хеша на сервере."
        footer={
          <>
            <Button variant="ghost" onClick={() => setPasswordOpen(false)} disabled={passwordBusy}>
              Отмена
            </Button>
            <Button variant="primary" onClick={() => void changePassword()} loading={passwordBusy}>
              Сменить пароль
            </Button>
          </>
        }
      >
        <div className="form-grid">
          <TextField
            label="Текущий пароль"
            type="password"
            autoComplete="current-password"
            value={passwordForm.current}
            onChange={(event) => setPasswordForm({ ...passwordForm, current: event.target.value })}
          />
          <TextField
            label="Новый пароль"
            type="password"
            autoComplete="new-password"
            value={passwordForm.next}
            onChange={(event) => setPasswordForm({ ...passwordForm, next: event.target.value })}
            hint="Минимум 8 символов"
          />
          <TextField
            label="Повторите новый пароль"
            type="password"
            autoComplete="new-password"
            value={passwordForm.confirm}
            onChange={(event) => setPasswordForm({ ...passwordForm, confirm: event.target.value })}
            error={passwordError}
          />
        </div>
        {passwordBusy && (
          <p className="muted inline-loading">
            <Spinner size={14} /> Сохраняем…
          </p>
        )}
      </Modal>
    </div>
  );
}
