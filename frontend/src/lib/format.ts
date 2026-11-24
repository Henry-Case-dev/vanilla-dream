const dayShort = ['Пн', 'Вт', 'Ср', 'Чт', 'Пт', 'Сб', 'Вс'];
const dayFull = [
  'Понедельник',
  'Вторник',
  'Среда',
  'Четверг',
  'Пятница',
  'Суббота',
  'Воскресенье',
];

export function dayShortName(index: number): string {
  return dayShort[index] ?? '';
}

export function dayFullName(index: number): string {
  return dayFull[index] ?? '';
}

export function isValidTimeZone(timeZone: string): boolean {
  if (!timeZone) return false;
  try {
    new Intl.DateTimeFormat('ru-RU', { timeZone }).format(new Date());
    return true;
  } catch {
    return false;
  }
}

export function toDate(value: string | null | undefined): Date | null {
  if (!value) return null;
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? null : date;
}

function format(value: string | null | undefined, timeZone: string | undefined, options: Intl.DateTimeFormatOptions): string {
  const date = toDate(value);
  if (!date) return '—';
  try {
    return new Intl.DateTimeFormat('ru-RU', { ...options, timeZone: timeZone || undefined }).format(date);
  } catch {
    return new Intl.DateTimeFormat('ru-RU', options).format(date);
  }
}

export function formatTime(value: string | null | undefined, timeZone?: string): string {
  return format(value, timeZone, { hour: '2-digit', minute: '2-digit' });
}

export function formatDateTime(value: string | null | undefined, timeZone?: string): string {
  return format(value, timeZone, { day: 'numeric', month: 'short', hour: '2-digit', minute: '2-digit' });
}

/** Через сколько времени наступит момент — без привязки к часовому поясу клиента. */
export function formatUntil(value: string | null | undefined): string {
  const date = toDate(value);
  if (!date) return '—';
  const diff = date.getTime() - Date.now();
  if (diff <= 0) return 'скоро';
  const minutes = Math.round(diff / 60000);
  if (minutes < 60) return `через ${minutes} мин`;
  const hours = Math.floor(minutes / 60);
  const rest = minutes % 60;
  if (hours < 24) return `через ${hours} ч${rest ? ` ${rest} мин` : ''}`;
  const days = Math.floor(hours / 24);
  return `через ${days} дн`;
}

/** «Сегодня, 18:30» / «Завтра, 09:00» / «25 нояб., 18:30» в часовом поясе станции. */
export function formatRelativeMoment(value: string | null | undefined, timeZone?: string): string {
  const date = toDate(value);
  if (!date) return '—';
  const zone = timeZone || undefined;
  const key = (d: Date) => {
    try {
      return new Intl.DateTimeFormat('en-CA', { timeZone: zone, year: 'numeric', month: '2-digit', day: '2-digit' }).format(d);
    } catch {
      return d.toISOString().slice(0, 10);
    }
  };
  const tomorrow = new Date(date.getTime() + 24 * 3600 * 1000);
  const yesterday = new Date(date.getTime() - 24 * 3600 * 1000);
  const now = new Date();
  const time = formatTime(value, timeZone);
  if (key(date) === key(now)) return `Сегодня, ${time}`;
  if (key(date) === key(tomorrow)) return `Завтра, ${time}`;
  if (key(date) === key(yesterday)) return `Вчера, ${time}`;
  return formatDateTime(value, timeZone);
}

/** Секунды → «3:42» или «1:02:05». */
export function formatDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds < 0) return '—';
  const total = Math.floor(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.floor((total % 3600) / 60);
  const s = total % 60;
  const pad = (part: number) => String(part).padStart(2, '0');
  return h > 0 ? `${h}:${pad(m)}:${pad(s)}` : `${m}:${pad(s)}`;
}

/** Секунды → «1 ч 12 мин» / «12 мин» для суммарных длительностей. */
export function formatLongDuration(seconds: number | null | undefined): string {
  if (seconds === null || seconds === undefined || !Number.isFinite(seconds) || seconds <= 0) return '0 мин';
  const total = Math.round(seconds);
  const h = Math.floor(total / 3600);
  const m = Math.round((total % 3600) / 60);
  if (h > 0 && m > 0) return `${h} ч ${m} мин`;
  if (h > 0) return `${h} ч`;
  return `${m} мин`;
}

export function formatBytes(bytes: number | null | undefined): string {
  if (bytes === null || bytes === undefined || !Number.isFinite(bytes) || bytes < 0) return '—';
  if (bytes < 1024) return `${bytes} Б`;
  const units = ['КБ', 'МБ', 'ГБ', 'ТБ'];
  let value = bytes / 1024;
  let unit = 0;
  while (value >= 1024 && unit < units.length - 1) {
    value /= 1024;
    unit += 1;
  }
  const fixed = value >= 100 ? value.toFixed(0) : value >= 10 ? value.toFixed(1) : value.toFixed(2);
  return `${fixed.replace('.', ',')} ${units[unit]}`;
}

export function formatCount(value: number | null | undefined): string {
  if (value === null || value === undefined || !Number.isFinite(value)) return '—';
  return new Intl.NumberFormat('ru-RU').format(value);
}

export function pluralRu(count: number, forms: [string, string, string]): string {
  const abs = Math.abs(count) % 100;
  const last = abs % 10;
  if (abs > 10 && abs < 20) return forms[2];
  if (last > 1 && last < 5) return forms[1];
  if (last === 1) return forms[0];
  return forms[2];
}

export function tracksLabel(count: number | null | undefined): string {
  if (count === null || count === undefined) return '—';
  return `${formatCount(count)} ${pluralRu(count, ['трек', 'трека', 'треков'])}`;
}

export function minutesLabel(minutes: number | null | undefined): string {
  if (minutes === null || minutes === undefined || !Number.isFinite(minutes)) return '—';
  const normalized = ((minutes % 1440) + 1440) % 1440;
  const h = Math.floor(normalized / 60);
  const m = normalized % 60;
  return `${String(h).padStart(2, '0')}:${String(m).padStart(2, '0')}`;
}

export function timeToMinutes(value: string): number {
  const [h, m] = value.split(':').map((part) => Number.parseInt(part, 10));
  if (!Number.isFinite(h) || !Number.isFinite(m)) return 0;
  return Math.min(23, Math.max(0, h)) * 60 + Math.min(59, Math.max(0, m));
}

/** Текущий день недели (0 = понедельник) и минуты от полуночи в часовом поясе станции. */
export function stationNow(timeZone: string | undefined): { day: number; minutes: number } {
  const date = new Date();
  const zone = timeZone && isValidTimeZone(timeZone) ? timeZone : undefined;
  const weekdayMap: Record<string, number> = { Mon: 0, Tue: 1, Wed: 2, Thu: 3, Fri: 4, Sat: 5, Sun: 6 };
  let day = (date.getDay() + 6) % 7;
  let minutes = date.getHours() * 60 + date.getMinutes();
  if (zone) {
    try {
      const parts = new Intl.DateTimeFormat('en-US', {
        timeZone: zone,
        weekday: 'short',
        hour: '2-digit',
        minute: '2-digit',
        hour12: false,
      }).formatToParts(date);
      const weekday = parts.find((part) => part.type === 'weekday')?.value ?? '';
      const hour = Number.parseInt(parts.find((part) => part.type === 'hour')?.value ?? '0', 10);
      const minute = Number.parseInt(parts.find((part) => part.type === 'minute')?.value ?? '0', 10);
      if (weekday in weekdayMap) day = weekdayMap[weekday];
      if (Number.isFinite(hour) && Number.isFinite(minute)) minutes = (hour % 24) * 60 + minute;
    } catch {
      /* некорректный пояс — используем локальное время */
    }
  }
  return { day, minutes };
}

export function intervalLabel(start: string, end: string): string {
  const overnight = timeToMinutes(end) <= timeToMinutes(start);
  return `${start}–${end}${overnight ? ' (через полночь)' : ''}`;
}

export function sourceKindLabel(kind: string | null | undefined): string {
  switch (kind) {
    case 'override':
      return 'Ручное переопределение';
    case 'schedule':
      return 'Расписание';
    case 'rotation':
      return 'Ротация';
    case 'playlist':
      return 'Плейлист';
    case 'library':
      return 'Вся медиатека';
    case 'manual':
      return 'Ручная очередь';
    default:
      return kind ? kind : 'Источник';
  }
}

export function statusText(status: string | null | undefined): string {
  switch (status) {
    case 'on_air':
      return 'В эфире';
    case 'starting':
      return 'Запуск';
    case 'reconnecting':
      return 'Переподключение';
    case 'no_music':
      return 'Нет музыки';
    case 'error':
      return 'Ошибка';
    case 'off':
      return 'Эфир остановлен';
    default:
      return 'Неизвестно';
  }
}

export function repeatLabel(mode: string | null | undefined): string {
  switch (mode) {
    case 'all':
      return 'Повтор плейлиста';
    case 'one':
      return 'Повтор трека';
    default:
      return 'Без повтора';
  }
}

export function rotationModeLabel(mode: string | null | undefined): string {
  switch (mode) {
    case 'by_count':
      return 'По количеству треков';
    case 'random':
      return 'Случайный выбор';
    default:
      return 'По порядку';
  }
}

export function transitionLabel(value: string | null | undefined): string {
  switch (value) {
    case 'immediate':
      return 'Немедленно';
    case 'after_track':
      return 'После трека';
    default:
      return value || 'После трека';
  }
}

const timeZoneCache = new Map<string, string[]>();

/** Список часовых поясов из Intl (без внешних данных). */
export function timeZoneOptions(): string[] {
  const cached = timeZoneCache.get('all');
  if (cached) return cached;
  let zones: string[] = [];
  try {
    const supported = (Intl as unknown as { supportedValuesOf?: (key: string) => string[] }).supportedValuesOf;
    if (typeof supported === 'function') zones = supported('timeZone');
  } catch {
    zones = [];
  }
  if (zones.length === 0) {
    zones = [
      'UTC',
      'Europe/Moscow',
      'Europe/Kaliningrad',
      'Europe/Samara',
      'Asia/Yekaterinburg',
      'Asia/Omsk',
      'Asia/Krasnoyarsk',
      'Asia/Irkutsk',
      'Asia/Yakutsk',
      'Asia/Vladivostok',
      'Asia/Magadan',
      'Asia/Kamchatka',
    ];
  }
  timeZoneCache.set('all', zones);
  return zones;
}
