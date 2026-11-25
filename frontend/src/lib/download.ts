/**
 * Скачивание по прямой серверной ссылке. Ответ приходит с
 * Content-Disposition: attachment, поэтому Blob в памяти браузера не нужен.
 */
export function triggerDownload(url: string): void {
  const link = document.createElement('a');
  link.href = url;
  link.rel = 'noopener';
  link.download = '';
  document.body.appendChild(link);
  link.click();
  link.remove();
}

/** Несколько файлов подряд — только по явному действию пользователя. */
export function triggerDownloads(urls: string[], intervalMs = 350): () => void {
  let index = 0;
  const timer = window.setInterval(() => {
    const url = urls[index];
    index += 1;
    if (!url) {
      window.clearInterval(timer);
      return;
    }
    triggerDownload(url);
    if (index >= urls.length) window.clearInterval(timer);
  }, intervalMs);
  return () => window.clearInterval(timer);
}
