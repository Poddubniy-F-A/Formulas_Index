"""Обходчик Crossref (https://api.crossref.org): статьи российских издателей."""

import json
import logging
import os
import re
import time
from typing import Iterator, List, Optional

from src.base_harvester import BaseHarvester, DocumentRecord
from src.pdf_resolver import looks_like_pdf_url

logger = logging.getLogger(__name__)

MEMBERS_CACHE = os.getenv('CROSSREF_MEMBERS_CACHE', '/crossref_members_ru.json')
MEMBERS_CACHE_TTL = 30 * 24 * 3600  # список участников обновляется раз в месяц
MEMBERS_PER_QUERY = 300  # одноимённые фильтры Crossref объединяются по ИЛИ


class CrossrefHarvester(BaseHarvester):
    """
    Crossref - реестр DOI, через который российские журналы регистрируют свои статьи.
    Российские издатели отбираются по полю location в реестре участников Crossref
    (около 1700 организаций).

    Ссылки на полный текст депонированы лишь примерно у четверти российских DOI, поэтому
    для остальных сохраняется страница статьи (https://doi.org/...), а PDF ищется на этапе
    загрузки по мета-тегу citation_pdf_url.
    """

    min_interval = 1.0
    page_size = 1000

    def __init__(self, db_session=None, member_ids: List[str] = None,
                 work_types=('journal-article',), **kwargs):
        """
        Args:
            member_ids: идентификаторы участников Crossref (по умолчанию - все российские)
            work_types: типы работ Crossref (None - все типы)
        """
        super().__init__('crossref', 'https://api.crossref.org', db_session, **kwargs)
        self._member_ids = member_ids
        self.work_types = work_types

    def fetch_russian_member_ids(self) -> List[str]:
        """Идентификаторы российских участников Crossref (с кешированием в файл)."""
        if os.path.exists(MEMBERS_CACHE) and time.time() - os.path.getmtime(MEMBERS_CACHE) < MEMBERS_CACHE_TTL:
            with open(MEMBERS_CACHE, encoding='utf-8') as f:
                return json.load(f)
        ids, cursor = [], '*'
        while cursor:
            message = self.fetch(f'{self.base_url}/members', params={'rows': 1000, 'cursor': cursor},
                               timeout=120).json()['message']
            items = message.get('items') or []
            ids += [str(m['id']) for m in items if re.search(r'russia', m.get('location') or '', re.IGNORECASE)]
            cursor = message.get('next-cursor') if items else None
        with open(MEMBERS_CACHE, 'w', encoding='utf-8') as f:
            json.dump(ids, f)
        logger.info(f'[crossref] российских участников: {len(ids)}')
        return ids

    @staticmethod
    def extract_pdf_url(item: dict) -> Optional[str]:
        """Извлекает прямую ссылку на PDF из элемента Crossref."""
        links = item.get('link') or []
        for link in links:
            if link.get('content-type') == 'application/pdf':
                return link['URL']
        for link in links:
            if looks_like_pdf_url(link.get('URL')):
                return link['URL']
        return None

    def crawl_for_docs(self, year: int) -> Iterator[DocumentRecord]:
        members = self._member_ids or self.fetch_russian_member_ids()
        for start in range(0, len(members), MEMBERS_PER_QUERY):
            batch = members[start:start + MEMBERS_PER_QUERY]
            filters = [f'member:{m}' for m in batch]
            filters += [f'from-pub-date:{year}-01-01', f'until-pub-date:{year}-12-31']
            filters += [f'type:{t}' for t in self.work_types or ()]
            params = {
                'filter': ','.join(filters),
                'select': 'DOI,title,link,URL,issued',
                'rows': self.page_size,
                'cursor': '*',
            }
            while params['cursor']:
                message = self.fetch(f'{self.base_url}/works', params=params, timeout=120).json()['message']
                if params['cursor'] == '*':
                    logger.info(f'[crossref] участники {start + 1}-{start + len(batch)} из {len(members)}: '
                                f'статей {message.get("total-results")}')
                items = message.get('items') or []
                if not items:
                    break
                for item in items:
                    try:
                        titles = item.get('title') or []
                        yield DocumentRecord(
                            external_id=item['DOI'],
                            doi=item['DOI'],
                            pdf_url=self.extract_pdf_url(item),
                            landing_url=item.get('URL'),
                            title=titles[0] if titles else None,
                            year=year,
                        )
                    except Exception as e:
                        logger.warning(f'[crossref] пропуск записи {item.get("DOI", "?")}: {e}')
                params['cursor'] = message.get('next-cursor')
