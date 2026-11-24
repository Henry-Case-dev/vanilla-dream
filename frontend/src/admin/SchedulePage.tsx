import { useMemo, useState } from 'react';
import { Link } from 'react-router-dom';
import {
  AlertTriangle,
  CalendarClock,
  Clock,
  Pencil,
  Plus,
  RefreshCw,
  Trash2,
} from 'lucide-react';
import {
  Button,
  Chip,
  IconButton,
  PageHeader,
  Panel,
  SelectField,
  Switch,
  TextField,
} from '../components/ui';
import { ConfirmDialog, Modal } from '../components/Modal';
import { EmptyState, ErrorState, LoadingState } from '../components/States';
import { useToast } from '../components/Toasts';
import { useLive } from '../lib/live';
import { useResource, useTick } from '../lib/hooks';
import { api, errorMessage } from '../lib/api';
import {
  dayFullName,
  dayShortName,
  formatRelativeMoment,
  formatTime,
  intervalLabel,
  minutesLabel,
  stationNow,
  timeToMinutes,
  transitionLabel,
} from '../lib/format';
import type { ScheduleRule, ScheduleRuleInput } from '../lib/types';

const HOUR_HEIGHT = 34;

interface RuleForm {
  name: string;
  useRotation: boolean;
  playlistId: string;
  days: number[];
  start: string;
  end: string;
  enabled: boolean;
  priority: string;
  transition: string;
}

const emptyForm = (day: number, transition: string): RuleForm => ({
  name: '',
  useRotation: true,
  playlistId: '',
  days: [day],
  start: '00:00',
  end: '01:00',
  enabled: true,
  priority: '0',
  transition,
});

function ruleContains(rule: ScheduleRule, day: number, minutes: number): boolean {
  if (!rule.enabled) return false;
  const start = timeToMinutes(rule.start);
  const end = timeToMinutes(rule.end);
  if (start === end) return false;
  if (end > start) return rule.days.includes(day) && minutes >= start && minutes < end;
  const previousDay = (day + 6) % 7;
  return (rule.days.includes(day) && minutes >= start) || (rule.days.includes(previousDay) && minutes < end);
}

function ruleIntervals(rule: ScheduleRule, day: number): Array<[number, number]> {
  const start = timeToMinutes(rule.start);
  const end = timeToMinutes(rule.end);
  if (start === end) return [];
  if (end > start) return rule.days.includes(day) ? [[start, end]] : [];
  const intervals: Array<[number, number]> = [];
  const previousDay = (day + 6) % 7;
  if (rule.days.includes(day)) intervals.push([start, 1440]);
  if (rule.days.includes(previousDay)) intervals.push([0, end]);
  return intervals;
}

function rulesOverlap(a: ScheduleRule, b: ScheduleRule): boolean {
  if (!a.enabled || !b.enabled) return false;
  for (let day = 0; day < 7; day += 1) {
    for (const [aStart, aEnd] of ruleIntervals(a, day)) {
      for (const [bStart, bEnd] of ruleIntervals(b, day)) {
        if (aStart < bEnd && bStart < aEnd) return true;
      }
    }
  }
  return false;
}

interface DayBlock {
  rule: ScheduleRule;
  top: number;
  height: number;
  continuation: boolean;
  active: boolean;
  colorIndex: number;
}

export function SchedulePage() {
  const toast = useToast();
  const { now } = useLive();
  useTick(15000);

  const schedule = useResource(() => api.admin.schedule.get(), []);
  const settings = useResource(() => api.admin.settings.get(), []);
  const playlists = useResource(() => api.admin.playlists.list(), []);
  const rotation = useResource(() => api.admin.rotation.get(), []);
  const conflictsApi = useResource(() => api.admin.schedule.conflicts(), []);

  const [modalOpen, setModalOpen] = useState(false);
  const [editing, setEditing] = useState<ScheduleRule | null>(null);
  const [form, setForm] = useState<RuleForm>(() => emptyForm(0, 'after_track'));
  const [saving, setSaving] = useState(false);
  const [ruleBusy, setRuleBusy] = useState<string | null>(null);
  const [deleteTarget, setDeleteTarget] = useState<ScheduleRule | null>(null);
  const [selectedDay, setSelectedDay] = useState<number | null>(null);
  const [scheduleToggleBusy, setScheduleToggleBusy] = useState(false);

  const timeZone = schedule.data?.timezone ?? settings.data?.timezone ?? 'UTC';
  const scheduleEnabled = schedule.data?.enabled ?? settings.data?.scheduleEnabled ?? true;
  const station = stationNow(timeZone);
  const dayForMobile = selectedDay ?? station.day;

  const rules = schedule.data?.rules ?? [];
  const playlistName = (id: string | null): string =>
    id ? playlists.data?.find((playlist) => playlist.id === id)?.name ?? 'Плейлист удалён' : 'Ротация';

  const activeRule = useMemo(() => {
    if (!scheduleEnabled) return null;
    const candidates = rules
      .filter((rule) => ruleContains(rule, station.day, station.minutes))
      .sort((a, b) => {
        if (b.priority !== a.priority) return b.priority - a.priority;
        if (timeToMinutes(a.start) !== timeToMinutes(b.start)) {
          return timeToMinutes(a.start) - timeToMinutes(b.start);
        }
        return a.id.localeCompare(b.id);
      });
    return candidates[0] ?? null;
  }, [rules, scheduleEnabled, station.day, station.minutes]);

  const clientConflicts = useMemo(() => {
    const pairs: Array<{ ids: string[]; message: string }> = [];
    for (let i = 0; i < rules.length; i += 1) {
      for (let j = i + 1; j < rules.length; j += 1) {
        const a = rules[i];
        const b = rules[j];
        if (!a || !b) continue;
        if (rulesOverlap(a, b)) {
          pairs.push({
            ids: [a.id, b.id],
            message: `«${a.name}» пересекается с «${b.name}»`,
          });
        }
      }
    }
    return pairs;
  }, [rules]);

  const conflicts = useMemo(() => {
    const list = [...clientConflicts];
    (conflictsApi.data ?? []).forEach((entry) => {
      if (!list.some((item) => item.message === entry.message)) {
        list.push({ ids: entry.ruleIds, message: entry.message });
      }
    });
    return list;
  }, [clientConflicts, conflictsApi.data]);

  const blocksByDay = useMemo(() => {
    const result: DayBlock[][] = Array.from({ length: 7 }, () => []);
    rules.forEach((rule, ruleIndex) => {
      if (!rule.enabled) return;
      const start = timeToMinutes(rule.start);
      const end = timeToMinutes(rule.end);
      if (start === end) return;
      const active = activeRule?.id === rule.id;
      for (let day = 0; day < 7; day += 1) {
        if (end > start) {
          if (rule.days.includes(day)) {
            result[day]?.push({
              rule,
              top: (start / 60) * HOUR_HEIGHT,
              height: ((end - start) / 60) * HOUR_HEIGHT,
              continuation: false,
              active,
              colorIndex: ruleIndex % 5,
            });
          }
        } else {
          if (rule.days.includes(day)) {
            result[day]?.push({
              rule,
              top: (start / 60) * HOUR_HEIGHT,
              height: ((1440 - start) / 60) * HOUR_HEIGHT,
              continuation: false,
              active,
              colorIndex: ruleIndex % 5,
            });
          }
          const previous = (day + 6) % 7;
          if (end > 0 && rule.days.includes(previous)) {
            result[day]?.push({
              rule,
              top: 0,
              height: (end / 60) * HOUR_HEIGHT,
              continuation: true,
              active,
              colorIndex: ruleIndex % 5,
            });
          }
        }
      }
    });
    return result;
  }, [rules, activeRule]);

  const nextChange = useMemo(() => {
    if (!scheduleEnabled || rules.length === 0) return null;
    const currentAbsolute = station.day * 1440 + station.minutes;
    let best: { absolute: number; rule: ScheduleRule; label: string } | null = null;
    const consider = (offsetDays: number, minutes: number, rule: ScheduleRule, kind: 'начало' | 'конец') => {
      const absolute = (station.day + offsetDays) * 1440 + minutes;
      if (absolute <= currentAbsolute) return;
      if (best && absolute >= best.absolute) return;
      const dayIndex = (station.day + offsetDays) % 7;
      const dayLabel = offsetDays === 0 ? 'сегодня' : offsetDays === 1 ? 'завтра' : dayShortName(dayIndex);
      best = {
        absolute,
        rule,
        label: `${kind} ${dayLabel}, ${minutesLabel(minutes)}`,
      };
    };
    for (let offset = 0; offset < 8; offset += 1) {
      const day = (station.day + offset) % 7;
      rules.forEach((rule) => {
        if (!rule.enabled) return;
        const start = timeToMinutes(rule.start);
        const end = timeToMinutes(rule.end);
        if (rule.days.includes(day)) consider(offset, start, rule, 'начало');
        const previous = (day + 6) % 7;
        const endApplies = end > start ? rule.days.includes(day) : rule.days.includes(previous);
        if (endApplies) consider(offset, end, rule, 'конец');
      });
    }
    return best as { absolute: number; rule: ScheduleRule; label: string } | null;
  }, [rules, scheduleEnabled, station.day, station.minutes]);

  const defaultSource = useMemo(() => {
    if (rotation.data?.items.some((item) => item.enabled)) return 'ротации плейлистов';
    const id = settings.data?.defaultPlaylistId;
    if (id) return `плейлиста «${playlistName(id)}»`;
    return 'всей медиатеки';
  }, [rotation.data, settings.data, playlists.data]);

  const openCreate = () => {
    setEditing(null);
    setForm(emptyForm(station.day, settings.data?.defaultTransition ?? 'after_track'));
    setModalOpen(true);
  };

  const openEdit = (rule: ScheduleRule) => {
    setEditing(rule);
    setForm({
      name: rule.name,
      useRotation: rule.useRotation,
      playlistId: rule.playlistId ?? '',
      days: rule.days,
      start: rule.start,
      end: rule.end,
      enabled: rule.enabled,
      priority: String(rule.priority),
      transition: rule.transition,
    });
    setModalOpen(true);
  };

  const saveRule = async () => {
    if (!form.name.trim()) {
      toast.notify('error', 'Введите название правила');
      return;
    }
    if (form.days.length === 0) {
      toast.notify('error', 'Выберите хотя бы один день недели');
      return;
    }
    if (form.start === form.end) {
      toast.notify('error', 'Начало и конец интервала не могут совпадать');
      return;
    }
    if (!form.useRotation && !form.playlistId) {
      toast.notify('error', 'Выберите плейлист для правила');
      return;
    }
    const payload: ScheduleRuleInput = {
      name: form.name.trim(),
      playlist_id: form.useRotation ? null : form.playlistId,
      use_rotation: form.useRotation,
      days: [...form.days].sort((a, b) => a - b),
      start: form.start,
      end: form.end,
      enabled: form.enabled,
      priority: Number.parseInt(form.priority, 10) || 0,
      transition: form.transition,
    };
    setSaving(true);
    try {
      if (editing) {
        await api.admin.schedule.update(editing.id, payload);
        toast.notify('success', 'Правило обновлено');
      } else {
        await api.admin.schedule.create(payload);
        toast.notify('success', 'Правило создано');
      }
      setModalOpen(false);
      void schedule.reload(true);
      void conflictsApi.reload(true);
      void settings.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setSaving(false);
    }
  };

  const toggleRuleEnabled = async (rule: ScheduleRule) => {
    setRuleBusy(rule.id);
    try {
      await api.admin.schedule.update(rule.id, { enabled: !rule.enabled });
      void schedule.reload(true);
      void conflictsApi.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setRuleBusy(null);
    }
  };

  const deleteRule = async () => {
    if (!deleteTarget) return;
    setRuleBusy(deleteTarget.id);
    try {
      await api.admin.schedule.remove(deleteTarget.id);
      toast.notify('success', 'Правило удалено');
      setDeleteTarget(null);
      void schedule.reload(true);
      void conflictsApi.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setRuleBusy(null);
    }
  };

  const toggleSchedule = async (enabled: boolean) => {
    setScheduleToggleBusy(true);
    try {
      await api.admin.settings.save({ schedule_enabled: enabled });
      toast.notify('success', enabled ? 'Расписание включено' : 'Расписание отключено');
      void settings.reload(true);
      void schedule.reload(true);
    } catch (caught) {
      toast.notify('error', errorMessage(caught));
    } finally {
      setScheduleToggleBusy(false);
    }
  };

  const toggleDay = (day: number, checked: boolean) => {
    setForm((current) => ({
      ...current,
      days: checked
        ? [...current.days, day].sort((a, b) => a - b)
        : current.days.filter((value) => value !== day),
    }));
  };

  if ((schedule.loading && !schedule.data) || (settings.loading && !settings.data)) {
    return (
      <div className="page">
        <PageHeader title="Расписание" subtitle="Недельная сетка и правила" />
        <LoadingState label="Загружаем расписание…" rows={4} />
      </div>
    );
  }

  if (schedule.error && !schedule.data) {
    return (
      <div className="page">
        <PageHeader title="Расписание" subtitle="Недельная сетка и правила" />
        <ErrorState message={schedule.error} onRetry={() => void schedule.reload()} />
      </div>
    );
  }

  return (
    <div className="page page--schedule">
      <PageHeader
        title="Расписание"
        subtitle={
          <span className="schedule-subtitle">
            <Clock size={14} aria-hidden="true" />
            Время станции: {formatTime(new Date().toISOString(), timeZone)} ·{' '}
            {dayFullName(station.day)} · {timeZone}
          </span>
        }
        actions={
          <>
            <IconButton
              icon={RefreshCw}
              label="Обновить расписание"
              title="Обновить расписание"
              onClick={() => {
                void schedule.reload();
                void conflictsApi.reload();
                void playlists.reload();
                void rotation.reload();
              }}
            />
            <Button variant="primary" icon={Plus} onClick={openCreate}>
              Новое правило
            </Button>
          </>
        }
      />

      <Panel className="schedule-toolbar">
        <Switch
          checked={scheduleEnabled}
          onChange={(checked) => void toggleSchedule(checked)}
          label="Расписание включено"
          description="Если выключено, применяется ротация или плейлист по умолчанию"
          disabled={scheduleToggleBusy}
        />
        <div className="schedule-toolbar__status">
          {activeRule ? (
            <Chip tone="accent">
              Активно: {activeRule.name} · {activeRule.start}–{activeRule.end}
            </Chip>
          ) : (
            <Chip tone="neutral">Активное правило отсутствует</Chip>
          )}
          {nextChange ? (
            <span className="muted">
              {nextChange.label} · {nextChange.rule.name}
            </span>
          ) : now?.nextScheduleChange ? (
            <span className="muted">
              {formatRelativeMoment(now.nextScheduleChange.at, timeZone)}
              {now.nextScheduleChange.ruleName ? ` · ${now.nextScheduleChange.ruleName}` : ''}
            </span>
          ) : null}
        </div>
      </Panel>

      {!scheduleEnabled && (
        <div className="banner banner--warning" role="status">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <strong>Расписание отключено</strong>
            <p>
              Эфир идёт по источнику по умолчанию: {defaultSource}.{' '}
              <Link to="/admin/playlists?tab=rotation">Настроить ротацию</Link> ·{' '}
              <Link to="/admin/settings">Изменить в настройках</Link>
            </p>
          </div>
        </div>
      )}

      {conflicts.length > 0 && (
        <div className="banner banner--warning" role="alert">
          <AlertTriangle size={16} aria-hidden="true" />
          <div>
            <strong>Пересечение правил</strong>
            <ul className="banner__list">
              {conflicts.map((conflict) => (
                <li key={conflict.message}>{conflict.message}</li>
              ))}
            </ul>
            <p>При пересечении применяется правило с большим приоритетом.</p>
          </div>
        </div>
      )}

      <Panel className="week-panel">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">Недельная сетка</h2>
            <p className="panel-subtitle">
              Часовой пояс станции {timeZone}. Ночные интервалы продолжаются на следующий день.
            </p>
          </div>
          <CalendarClock size={18} className="muted" aria-hidden="true" />
        </div>

        <div className={`week ${scheduleEnabled ? '' : 'is-disabled'}`.trim()}>
          <div className="week__head">
            <div className="week__head-axis">Время</div>
            {Array.from({ length: 7 }).map((_, day) => (
              <div key={day} className={`week__head-day ${day === station.day ? 'is-today' : ''}`.trim()}>
                <span className="week__head-short">{dayShortName(day)}</span>
                <span className="week__head-full">{dayFullName(day)}</span>
              </div>
            ))}
          </div>
          <div className="week__body">
            <div className="week__grid" style={{ height: 24 * HOUR_HEIGHT }}>
              <div className="week__axis" aria-hidden="true">
                {Array.from({ length: 24 }).map((_, hour) => (
                  <div key={hour} className="week__hour" style={{ top: hour * HOUR_HEIGHT }}>
                    <span>{String(hour).padStart(2, '0')}:00</span>
                  </div>
                ))}
              </div>
              {Array.from({ length: 7 }).map((_, day) => (
                <div key={day} className={`week__day ${day === station.day ? 'is-today' : ''}`.trim()}>
                  {Array.from({ length: 24 }).map((__, hour) => (
                    <span key={hour} className="week__line" style={{ top: hour * HOUR_HEIGHT }} aria-hidden="true" />
                  ))}
                  {(blocksByDay[day] ?? []).map((block, index) => (
                    <div
                      key={`${block.rule.id}-${block.continuation ? 'c' : 's'}-${index}`}
                      className={`week__block week__block--c${block.colorIndex} ${
                        block.active ? 'is-active' : ''
                      } ${block.continuation ? 'is-continuation' : ''}`.trim()}
                      style={{ top: block.top, height: Math.max(20, block.height) }}
                      title={`${block.rule.name} · ${block.rule.start}–${block.rule.end}${
                        block.continuation ? ' (продолжение)' : ''
                      }`}
                    >
                      <span className="week__block-name">{block.rule.name}</span>
                      <span className="week__block-time">
                        {block.rule.start}–{block.rule.end}
                      </span>
                      <span className="week__block-source">{playlistName(block.rule.playlistId)}</span>
                    </div>
                  ))}
                  {day === station.day && (
                    <div className="week__now" style={{ top: (station.minutes / 60) * HOUR_HEIGHT }}>
                      <span className="week__now-label">{minutesLabel(station.minutes)}</span>
                    </div>
                  )}
                </div>
              ))}
            </div>
          </div>
        </div>

        {rules.length === 0 && (
          <EmptyState
            compact
            icon={CalendarClock}
            title="Правил пока нет"
            description="Создайте правило, чтобы задать программу по дням и часам."
            action={
              <Button variant="primary" size="sm" icon={Plus} onClick={openCreate}>
                Новое правило
              </Button>
            }
          />
        )}
      </Panel>

      <Panel className="day-panel">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">День недели</h2>
            <p className="panel-subtitle">Правила по дням недели</p>
          </div>
        </div>
        <div className="day-tabs" role="tablist" aria-label="День недели">
          {Array.from({ length: 7 }).map((_, day) => (
            <button
              key={day}
              type="button"
              role="tab"
              aria-selected={day === dayForMobile}
              className={`tab tab--day ${day === dayForMobile ? 'is-active' : ''}`.trim()}
              onClick={() => setSelectedDay(day)}
            >
              {dayShortName(day)}
            </button>
          ))}
        </div>
        <div className="day-list">
          {rules.filter((rule) => ruleIntervals(rule, dayForMobile).length > 0).length === 0 ? (
            <EmptyState compact title="На этот день правил нет" description="Эфир пойдёт по источнику по умолчанию." />
          ) : (
            rules
              .filter((rule) => ruleIntervals(rule, dayForMobile).length > 0)
              .sort((a, b) => timeToMinutes(a.start) - timeToMinutes(b.start))
              .map((rule) => {
                const active = activeRule?.id === rule.id && dayForMobile === station.day;
                return (
                  <div key={rule.id} className={`day-rule ${active ? 'is-active' : ''}`.trim()}>
                    <div className="day-rule__time">
                      {intervalLabel(rule.start, rule.end)}
                    </div>
                    <div className="day-rule__text">
                      <span className="day-rule__name">{rule.name}</span>
                      <span className="day-rule__source">
                        {rule.useRotation ? 'Ротация' : playlistName(rule.playlistId)} · приоритет{' '}
                        {rule.priority} · {transitionLabel(rule.transition)}
                      </span>
                    </div>
                    <div className="day-rule__actions">
                      <IconButton icon={Pencil} label="Изменить правило" size="sm" onClick={() => openEdit(rule)} />
                      <IconButton
                        icon={Trash2}
                        label="Удалить правило"
                        size="sm"
                        tone="danger"
                        onClick={() => setDeleteTarget(rule)}
                      />
                    </div>
                  </div>
                );
              })
          )}
        </div>
      </Panel>

      <Panel className="rules-panel">
        <div className="panel-head">
          <div>
            <h2 className="panel-title">Правила расписания</h2>
            <p className="panel-subtitle">Приоритет, дни и переходы</p>
          </div>
          <Button variant="secondary" size="sm" icon={Plus} onClick={openCreate}>
            Добавить
          </Button>
        </div>

        {rules.length === 0 ? (
          <EmptyState compact title="Список правил пуст" description="Пока действует источник по умолчанию." />
        ) : (
          <div className="table-scroll">
            <table className="table rules-table">
              <thead>
                <tr>
                  <th>Правило</th>
                  <th>Дни</th>
                  <th>Интервал</th>
                  <th>Источник</th>
                  <th>Приоритет</th>
                  <th>Переход</th>
                  <th>Вкл.</th>
                  <th aria-label="Действия" />
                </tr>
              </thead>
              <tbody>
                {rules.map((rule) => (
                  <tr key={rule.id} className={activeRule?.id === rule.id ? 'is-selected' : undefined}>
                    <td>
                      <span className="rules-table__name">
                        {rule.name}
                        {activeRule?.id === rule.id && <Chip tone="accent">сейчас</Chip>}
                      </span>
                    </td>
                    <td>
                      <span className="rules-table__days">
                        {rule.days.map((day) => (
                          <span key={day} className="day-dot">
                            {dayShortName(day)}
                          </span>
                        ))}
                      </span>
                    </td>
                    <td className="rules-table__time">{intervalLabel(rule.start, rule.end)}</td>
                    <td>{rule.useRotation ? 'Ротация' : playlistName(rule.playlistId)}</td>
                    <td>{rule.priority}</td>
                    <td>{transitionLabel(rule.transition)}</td>
                    <td>
                      <Switch
                        checked={rule.enabled}
                        label=""
                        ariaLabel={`???????? ??????? ${rule.name}`}
                        onChange={() => void toggleRuleEnabled(rule)}
                        disabled={ruleBusy === rule.id}
                      />
                    </td>
                    <td>
                      <div className="rules-table__actions">
                        <IconButton icon={Pencil} label="Изменить правило" size="sm" onClick={() => openEdit(rule)} />
                        <IconButton
                          icon={Trash2}
                          label="Удалить правило"
                          size="sm"
                          tone="danger"
                          onClick={() => setDeleteTarget(rule)}
                        />
                      </div>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        )}
      </Panel>

      <Modal
        open={modalOpen}
        onClose={() => (saving ? undefined : setModalOpen(false))}
        title={editing ? 'Изменить правило' : 'Новое правило'}
        description="Время указывается в часовом поясе станции. Конец раньше начала означает переход через полночь."
        size="lg"
        footer={
          <>
            <Button variant="ghost" onClick={() => setModalOpen(false)} disabled={saving}>
              Отмена
            </Button>
            <Button variant="primary" onClick={() => void saveRule()} loading={saving}>
              Сохранить
            </Button>
          </>
        }
      >
        <div className="form-grid form-grid--2">
          <TextField
            label="Название"
            value={form.name}
            onChange={(event) => setForm({ ...form, name: event.target.value })}
            placeholder="Например: Ночной эфир"
          />
          <TextField
            label="Приоритет"
            type="number"
            value={form.priority}
            onChange={(event) => setForm({ ...form, priority: event.target.value })}
            hint="Больше — важнее при пересечении"
          />

          <SelectField
            label="Источник"
            value={form.useRotation ? 'rotation' : 'playlist'}
            onChange={(event) =>
              setForm({
                ...form,
                useRotation: event.target.value === 'rotation',
                playlistId: event.target.value === 'rotation' ? '' : form.playlistId,
              })
            }
          >
            <option value="rotation">Ротация плейлистов</option>
            <option value="playlist">Конкретный плейлист</option>
          </SelectField>

          <SelectField
            label="Плейлист"
            value={form.playlistId}
            onChange={(event) => setForm({ ...form, playlistId: event.target.value })}
            disabled={form.useRotation}
            hint={form.useRotation ? 'Для ротации плейлист не выбирается' : undefined}
          >
            <option value="">Выберите плейлист</option>
            {(playlists.data ?? []).map((playlist) => (
              <option key={playlist.id} value={playlist.id}>
                {playlist.name}
              </option>
            ))}
          </SelectField>

          <TextField
            label="Начало"
            type="time"
            value={form.start}
            onChange={(event) => setForm({ ...form, start: event.target.value })}
          />
          <TextField
            label="Конец"
            type="time"
            value={form.end}
            onChange={(event) => setForm({ ...form, end: event.target.value })}
          />

          <SelectField
            label="Переход"
            value={form.transition}
            onChange={(event) => setForm({ ...form, transition: event.target.value })}
          >
            <option value="after_track">После трека</option>
            <option value="immediate">Немедленно</option>
          </SelectField>

          <div className="form-inline">
            <Switch
              checked={form.enabled}
              onChange={(checked) => setForm({ ...form, enabled: checked })}
              label="Правило включено"
            />
          </div>

          <div className="form-span">
            <span className="field__label">Дни недели</span>
            <div className="day-picker">
              {Array.from({ length: 7 }).map((_, day) => {
                const checked = form.days.includes(day);
                return (
                  <button
                    key={day}
                    type="button"
                    className={`day-chip ${checked ? 'is-active' : ''}`.trim()}
                    aria-pressed={checked}
                    onClick={() => toggleDay(day, !checked)}
                  >
                    {dayShortName(day)}
                  </button>
                );
              })}
            </div>
            <span className="field__hint">Можно выбрать несколько дней</span>
          </div>
        </div>
      </Modal>

      <ConfirmDialog
        open={deleteTarget !== null}
        title="Удалить правило?"
        description={deleteTarget ? `Правило «${deleteTarget.name}» будет удалено из расписания.` : null}
        confirmLabel="Удалить"
        danger
        busy={ruleBusy !== null}
        onConfirm={() => void deleteRule()}
        onCancel={() => setDeleteTarget(null)}
      />
    </div>
  );
}
