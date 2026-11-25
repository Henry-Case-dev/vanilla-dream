import { BrowserRouter, Link, Route, Routes } from 'react-router-dom';
import { ErrorBoundary } from './components/ErrorBoundary';
import { ToastProvider } from './components/Toasts';
import { AuthProvider, RequireAuth } from './lib/auth';
import { UploadProvider } from './lib/uploads';
import { ExportProvider } from './lib/exports';
import { LiveProvider } from './lib/live';
import { PlaylistSettingsProvider } from './lib/playlist-settings';
import { AudioProvider } from './lib/audio';
import { AdminLayout } from './admin/AdminLayout';
import { LoginPage } from './admin/LoginPage';
import { OverviewPage } from './admin/OverviewPage';
import { LibraryPage } from './admin/LibraryPage';
import { PlaylistsPage } from './admin/PlaylistsPage';
import { PlaylistDetailPage } from './admin/PlaylistDetailPage';
import { SchedulePage } from './admin/SchedulePage';
import { SettingsPage } from './admin/SettingsPage';
import { PublicPlayerPage } from './public/PublicPlayerPage';

function NotFoundPage() {
  return (
    <div className="fatal">
      <h1>Страница не найдена</h1>
      <p>Адрес изменился или введён с ошибкой. Вернитесь к плееру или в панель управления.</p>
      <div className="state__action">
        <Link className="btn btn--primary btn--md" to="/">
          <span className="btn__label">Публичный плеер</span>
        </Link>
        <Link className="btn btn--ghost btn--md" to="/admin">
          <span className="btn__label">Панель управления</span>
        </Link>
      </div>
    </div>
  );
}

export function App() {
  return (
    <ErrorBoundary>
      <BrowserRouter>
        <ToastProvider>
          <AuthProvider>
            <UploadProvider>
              <ExportProvider>
                <LiveProvider>
                  <PlaylistSettingsProvider>
                  <AudioProvider>
                  <Routes>
                    <Route path="/" element={<PublicPlayerPage />} />
                    <Route path="/admin/login" element={<LoginPage />} />
                    <Route
                      path="/admin"
                      element={
                        <RequireAuth>
                          <AdminLayout />
                        </RequireAuth>
                      }
                    >
                      <Route index element={<OverviewPage />} />
                      <Route path="library" element={<LibraryPage />} />
                      <Route path="playlists" element={<PlaylistsPage />} />
                      <Route path="playlists/:id" element={<PlaylistDetailPage />} />
                      <Route path="schedule" element={<SchedulePage />} />
                      <Route path="settings" element={<SettingsPage />} />
                    </Route>
                    <Route path="*" element={<NotFoundPage />} />
                  </Routes>
                  </AudioProvider>
                  </PlaylistSettingsProvider>
                </LiveProvider>
              </ExportProvider>
            </UploadProvider>
          </AuthProvider>
        </ToastProvider>
      </BrowserRouter>
    </ErrorBoundary>
  );
}
