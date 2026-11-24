import { useState } from 'react';
import { ExternalLink } from 'lucide-react';
import type { RadioLink } from '../lib/types';
import { CopyButton } from './CopyButton';
import { EmptyState } from './States';

interface LinkListProps {
  links: RadioLink[];
  emptyHint?: string;
}

function LinkRow({ link }: { link: RadioLink }) {
  const [expanded, setExpanded] = useState(false);

  return (
    <div className={`link-row ${link.primary ? 'link-row--primary' : ''}`.trim()}>
      <div className="link-row__text">
        <span className="link-row__label">{link.label}</span>
        {link.hint && <span className="link-row__hint">{link.hint}</span>}
      </div>
      <button
        type="button"
        className={`link-row__url ${expanded ? 'is-expanded' : ''}`.trim()}
        title={expanded ? 'Свернуть адрес' : `Показать полностью: ${link.url}`}
        onClick={() => setExpanded((value) => !value)}
      >
        {link.url}
      </button>
      <div className="link-row__actions">
        <CopyButton value={link.url} showLabel={link.primary} />
        <a
          className="icon-btn icon-btn--sm"
          href={link.url}
          target="_blank"
          rel="noreferrer noopener"
          aria-label={`Открыть ${link.label} в новой вкладке`}
          title="Открыть"
        >
          <ExternalLink size={14} aria-hidden="true" />
        </a>
      </div>
    </div>
  );
}

/** Блок ссылок на трансляцию — основной рабочий блок панели. */
export function LinkList({ links, emptyHint = 'Сервер ещё не сформировал ссылки' }: LinkListProps) {
  if (links.length === 0) {
    return <EmptyState compact title="Ссылки недоступны" description={emptyHint} />;
  }
  return (
    <div className="link-list">
      {links.map((link, index) => (
        <LinkRow
          key={`${link.id}-${link.url}`}
          link={!links.some((entry) => entry.primary) && index === 0 ? { ...link, primary: true } : link}
        />
      ))}
    </div>
  );
}
