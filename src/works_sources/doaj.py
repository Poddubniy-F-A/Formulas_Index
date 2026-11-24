"""Обходчик DOAJ - Directory of Open Access Journals (https://doaj.org/api)."""

import logging
from typing import Iterator, Optional
from urllib.parse import quote

from src.base_harvester import BaseHarvester, DocumentRecord
from src.pdf_resolver import looks_like_pdf_url

logger = logging.getLogger(__name__)


class DoajHarvester(BaseHarvester):
    """
    DOAJ - каталог журналов открытого доступа; метаданные статей присылают сами журналы.
    Почти все российские журналы DOAJ регистрируют DOI в Crossref, поэтому DOAJ добавляет:
    - статьи без DOI;
    - прямые ссылки на полный текст от журнала (к работе, известной по DOI, добавляется копия).
    """

    min_interval = 0.5
    page_size = 100
    max_results = 10000  # поисковый API DOAJ не отдаёт записи глубже этой позиции

    def __init__(self, db_session=None, country: Optional[str] = 'RU', subject: Optional[str] = None, **kwargs):
        """
        Args:
            country: код страны издателя журнала (None - все страны)
            subject: тематика журнала по классификатору DOAJ, например 'mathematics' (None - любая)
        """
        super().__init__('doaj', 'https://doaj.org/api', db_session, **kwargs)
        self.country = country
        self.subject = subject

    @staticmethod
    def extract_links(bibjson: dict):
        """Извлекает прямую ссылку на PDF и страницу документа из bibjson."""
        fulltext = [link for link in bibjson.get('link') or [] if link.get('type') == 'fulltext' and link.get('url')]
        pdf = next((link['url'] for link in fulltext if (link.get('content_type') or '').lower() == 'pdf'), None)
        pdf = pdf or next((link['url'] for link in fulltext if looks_like_pdf_url(link['url'])), None)
        landing = next((link['url'] for link in fulltext if link['url'] != pdf), None)
        return pdf, landing

    @staticmethod
    def extract_doi(bibjson: dict) -> Optional[str]:
        """Извлекает DOI из списка идентификаторов bibjson."""
        for identifier in bibjson.get('identifier') or []:
            if (identifier.get('type') or '').lower() == 'doi' and identifier.get('id'):
                return identifier['id']
        return None

    def crawl_for_docs(self, year: int) -> Iterator[DocumentRecord]:
        conditions = [f'bibjson.year:{year}']
        if self.country:
            conditions.append(f'bibjson.journal.country:{self.country}')
        if self.subject:
            conditions.append(f'bibjson.subject.term:{self.subject}')
        url = f"{self.base_url}/search/articles/{quote(' AND '.join(conditions))}"

        page = 1
        while page * self.page_size <= self.max_results:
            data = self.fetch(url, params={'page': page, 'pageSize': self.page_size}).json()
            if page == 1:
                logger.info(f'[doaj] {" AND ".join(conditions)}: всего статей {data.get("total")}')
            results = data.get('results') or []
            if not results:
                return
            for article in results:
                try:
                    bibjson = article.get('bibjson') or {}
                    pdf, landing = self.extract_links(bibjson)
                    yield DocumentRecord(
                        external_id=article['id'],
                        pdf_url=pdf,
                        landing_url=landing,
                        title=bibjson.get('title'),
                        doi=self.extract_doi(bibjson),
                        year=year,
                    )
                except Exception as e:
                    logger.warning(f'[doaj] пропуск записи {article.get("id", "?")}: {e}')
            page += 1
