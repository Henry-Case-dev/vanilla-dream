import type { StationStatus } from './types';

export type StatusTone = 'live' | 'pending' | 'idle' | 'error';

export interface StatusPresentation {
  /** Полная подпись статуса с учётом причины. */
  label: string;
  /** Компактная подпись для нижней панели и сайдбара. */
  shortLabel: string;
  /** Понятное объяснение для пустых состояний и подсказок. */
  detail: string;
  tone: StatusTone;
  isLive: boolean;
  isProblem: boolean;
  reason: string | null;
}

interface ReasonCopy {
  label: string;
  shortLabel: string;
  detail: string;
}

const REASON_COPY: Record<string, ReasonCopy> = {
  stopped_by_owner: {
    label: 'Эфир остановлен',
    shortLabel: 'Остановлен',
    detail: 'Станция не вещает. Запустите эфир кнопкой в нижней панели.',
  },
  engine_starting: {
    label: 'Эфир запускается…',
    shortLabel: 'Запуск',
    detail: 'Движок запускается, поток появится через несколько секунд.',
  },
  playing: {
    label: 'В эфире',
    shortLabel: 'В эфире',
    detail: 'Движок сообщает текущий трек.',
  },
  library_empty: {
    label: 'Тишина: медиатека пуста',
    shortLabel: 'Медиатека пуста',
    detail: 'В медиатеке нет доступных треков. Загрузите музыку, чтобы эфир зазвучал.',
  },
  playlist_empty: {
    label: 'Тишина: плейлист пуст',
    shortLabel: 'Пустой плейлист',
    detail: 'Выбранный плейлист не содержит доступных треков. Добавьте музыку или выберите другой источник.',
  },
  playlist_exhausted: {
    label: 'Плейлист завершён',
    shortLabel: 'Плейлист завершён',
    detail: 'Плейлист проигран до конца без повтора. Запустите его снова или включите повтор.',
  },
  no_source: {
    label: 'Нет источника музыки',
    shortLabel: 'Нет источника',
    detail: 'Расписание, ротация и плейлист по умолчанию не дают доступного источника.',
  },
  mount_missing: {
    label: 'Поток переподключается…',
    shortLabel: 'Переподключение',
    detail: 'Поток временно отсутствует, движок восстанавливает вещание.',
  },
  engine_unavailable: {
    label: 'Движок недоступен',
    shortLabel: 'Движок недоступен',
    detail: 'Liquidsoap не отвечает. Проверьте состояние службы и последнюю ошибку.',
  },
  engine_sync_failed: {
    label: 'Очередь не передана движку',
    shortLabel: 'Сбой очереди',
    detail: 'Движок не принял подготовленную очередь. Требуется перезапуск движка.',
  },
  playback_stalled: {
    label: 'Эфир остановился',
    shortLabel: 'Эфир остановился',
    detail: 'Нет сигнала о старте трека дольше 60 секунд при непустой очереди.',
  },
  stats_unavailable: {
    label: 'Статистика недоступна',
    shortLabel: 'Нет статистики',
    detail: 'Icecast не отвечает на запрос статистики; число слушателей неизвестно.',
  },
};

function copyFor(status: StationStatus, reason: string | null): ReasonCopy {
  if (reason && REASON_COPY[reason]) return REASON_COPY[reason];
  switch (status) {
    case 'on_air':
      return REASON_COPY.playing;
    case 'off':
      return REASON_COPY.stopped_by_owner;
    case 'starting':
      return {
        label: 'Эфир запускается…',
        shortLabel: 'Запуск',
        detail: 'Движок запускается, поток появится через несколько секунд.',
      };
    case 'reconnecting':
      return {
        label: 'Поток переподключается…',
        shortLabel: 'Переподключение',
        detail: 'Поток временно отсутствует, движок восстанавливает вещание.',
      };
    case 'no_music':
      return {
        label: 'Тишина в эфире',
        shortLabel: 'Нет музыки',
        detail: 'Источник не даёт доступной музыки. Проверьте медиатеку и текущий источник.',
      };
    case 'error':
      return {
        label: 'Ошибка эфира',
        shortLabel: 'Ошибка',
        detail: 'Движок сообщил об ошибке. Проверьте службы и журнал.',
      };
    default:
      return {
        label: 'Состояние неизвестно',
        shortLabel: 'Нет данных',
        detail: 'Сервер ещё не сообщил состояние эфира.',
      };
  }
}

export function describeStationStatus(
  status: StationStatus | null | undefined,
  reason?: string | null,
): StatusPresentation {
  const normalizedStatus: StationStatus = status ?? 'unknown';
  const normalizedReason = reason && reason.trim() ? reason : null;
  const copy = copyFor(normalizedStatus, normalizedReason);
  const tone: StatusTone =
    normalizedStatus === 'on_air'
      ? 'live'
      : normalizedStatus === 'starting' || normalizedStatus === 'reconnecting'
        ? 'pending'
        : normalizedStatus === 'error'
          ? 'error'
          : 'idle';
  return {
    label: copy.label,
    shortLabel: copy.shortLabel,
    detail: copy.detail,
    tone,
    isLive: normalizedStatus === 'on_air',
    isProblem: normalizedStatus === 'error' || normalizedStatus === 'no_music',
    reason: normalizedReason,
  };
}

export function statusToneClass(tone: StatusTone): string {
  switch (tone) {
    case 'live':
      return 'is-live';
    case 'pending':
      return 'is-pending';
    case 'error':
      return 'is-error';
    default:
      return 'is-idle';
  }
}

export function statusDotClass(tone: StatusTone): string {
  switch (tone) {
    case 'live':
      return 'dot--live';
    case 'error':
      return 'dot--error';
    case 'pending':
      return 'dot--pending';
    default:
      return 'dot--idle';
  }
}
