import { Link, NavLink, Outlet } from 'react-router-dom';
import {
  CalendarClock,
  LayoutDashboard,
  Library,
  ListMusic,
  LogOut,
  Settings,
} from 'lucide-react';
import type { LucideIcon } from 'lucide-react';
import { Logo } from '../components/Logo';
import { useAuth } from '../lib/auth';
import { useLive } from '../lib/live';
import { useResource } from '../lib/hooks';
import { api } from '../lib/api';
import { describeStationStatus, statusDotClass } from '../lib/status';
import { useDataChanged } from '../lib/refresh';
import { PlayerBar } from './PlayerBar';

interface NavItem {
  to: string;
  label: string;
  icon: LucideIcon;
  end?: boolean;
}

const navItems: NavItem[] = [
  { to: '/admin', label: 'Обзор', icon: LayoutDashboard, end: true },
  { to: '/admin/library', label: 'Медиатека', icon: Library },
  { to: '/admin/playlists', label: 'Плейлисты', icon: ListMusic },
  { to: '/admin/schedule', label: 'Расписание', icon: CalendarClock },
  { to: '/admin/settings', label: 'Настройки', icon: Settings },
];

export function AdminLayout() {
  const { logout } = useAuth();
  const { now } = useLive();
  const playlists = useResource(() => api.admin.playlists.list(), [], { pollMs: 60000 });
  useDataChanged(() => void playlists.reload(true));

  const statusView = describeStationStatus(now?.station.status, now?.station.reason);

  const handleLogout = async () => {
    await logout();
  };

  return (
    <div className="app-shell">
      <aside className="sidebar">
        <div className="sidebar__brand">
          <Link to="/admin" aria-label="Vanilla Dream Radio — обзор">
            <Logo />
          </Link>
        </div>

        <nav className="nav" aria-label="Разделы панели">
          {navItems.map((item) => (
            <NavLink
              key={item.to}
              to={item.to}
              end={item.end}
              className={({ isActive }) => `nav__item ${isActive ? 'is-active' : ''}`.trim()}
            >
              <item.icon size={18} aria-hidden="true" />
              <span>{item.label}</span>
            </NavLink>
          ))}
        </nav>

        <div className="sidebar__section">
          <div className="sidebar__section-head">
            <span>Плейлисты</span>
            <Link to="/admin/playlists" className="sidebar__all">
              Все
            </Link>
          </div>
          {playlists.loading && !playlists.data ? (
            <div className="sidebar__skeleton" aria-hidden="true">
              <span className="skeleton" />
              <span className="skeleton" />
            </div>
          ) : playlists.error ? (
            <p className="sidebar__note">Список недоступен</p>
          ) : playlists.data && playlists.data.length > 0 ? (
            <ul className="sidebar__playlists">
              {playlists.data.slice(0, 8).map((playlist) => (
                <li key={playlist.id}>
                  <NavLink
                    to={`/admin/playlists/${playlist.id}`}
                    className={({ isActive }) => `sidebar__playlist ${isActive ? 'is-active' : ''}`.trim()}
                  >
                    <span className="sidebar__playlist-name">{playlist.name}</span>
                    {playlist.system && (
                      <span className="sidebar__playlist-system">системный</span>
                    )}
                  </NavLink>
                </li>
              ))}
            </ul>
          ) : (
            <p className="sidebar__note">Плейлистов пока нет</p>
          )}
        </div>

        <div className="sidebar__footer">
          <span className="sidebar__status" title={statusView.detail}>
            <span className={`dot ${statusDotClass(statusView.tone)}`} aria-hidden="true" />
            {now ? statusView.shortLabel : 'Нет данных'}
          </span>
          <div className="sidebar__footer-links">
            <Link to="/" className="sidebar__footer-link">
              Публичный плеер
            </Link>
            <button type="button" className="sidebar__footer-link" onClick={() => void handleLogout()}>
              <LogOut size={14} aria-hidden="true" />
              Выйти
            </button>
          </div>
        </div>
      </aside>

      <main className="main">
        <div className="main__inner">
          <Outlet />
        </div>
      </main>

      <PlayerBar />
    </div>
  );
}
