"""Базовый класс обходчиков источников (этап сбора метаданных и ссылок).

Ответственность класса:
- iter_documents(year) — генератор DocumentRecord (реализуют наследники)
- crawl(year, limit) — отфильтрованный поток записей с ссылками
- save_records(records, batch_size) — пакетное сохранение в БД с upsert
- harvest(year, limit, batch_size) — полный цикл обход + сохранение

Поиск и скачивание PDF выполняются отдельным этапом (src.downloader).
"""

import logging
import re
import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Iterable, Iterator, List, Optional

import requests
import urllib3.util.retry

from src.db import WorkSource, Work
from src.pdf_resolver import normalize_pdf_url
from src.session import RoutedSession

logger = logging.getLogger(__name__)


def normalize_doi(doi: Optional[str]) -> Optional[str]:
    """Приводит DOI к каноническому виду (lowercase, без префиксов)."""
    if not doi:
        return None
    doi = re.sub(r'^(https?://(dx\.)?doi\.org/|doi:)', '', doi.strip(), flags=re.IGNORECASE).lower()
    return doi if doi.startswith('10.') else None


@dataclass
class DocumentRecord:
    """
    Копия документа, найденная в источнике. Нужна хотя бы одна из ссылок:
    pdf_url - прямая ссылка на файл, landing_url - страница документа, где PDF
    будет найден на этапе загрузки.
    """
    external_id: str  # идентификатор документа в источнике
    pdf_url: Optional[str] = None
    landing_url: Optional[str] = None
    title: Optional[str] = None
    doi: Optional[str] = None
    year: Optional[int] = None

    def __post_init__(self):
        self.pdf_url = normalize_pdf_url(self.pdf_url)
        self.doi = normalize_doi(self.doi)
        if not self.landing_url and self.doi:
            self.landing_url = f'https://doi.org/{self.doi}'


@dataclass
class HarvestStats:
    found: int = 0  # записей с хотя бы одной ссылкой
    new_works: int = 0  # новых работ
    new_links: int = 0  # новых копий уже известных работ (совпадение по DOI)
    known: int = 0  # копия уже была в БД

    def merge(self, other: 'HarvestStats') -> None:
        self.found += other.found
        self.new_works += other.new_works
        self.new_links += other.new_links
        self.known += other.known


class BaseHarvester(ABC):
    """Абстрактный базовый класс обходчиков."""

    #: минимальная пауза между запросами к API источника, секунд
    min_interval: float = 1.0
    #: таймаут запроса к API источника, секунд
    api_timeout: int = 30
    #: размер партии при пакетном сохранении в БД
    batch_size: int = 50

    def __init__(self, source_name: str, base_url: str, db_session=None):
        self.source_name = source_name
        self.base_url = base_url
        self.db_session = db_session
        self.session = self._create_http_session()
        self._last_request = 0.0

    @staticmethod
    def _create_http_session() -> RoutedSession:
        """Создаёт HTTP-сессию с повторными попытками."""
        session = RoutedSession()
        adapter = requests.adapters.HTTPAdapter(max_retries=urllib3.util.retry.Retry(
            total=2, backoff_factor=1,
            status_forcelist=[429, 500, 502, 503, 504],
            allowed_methods=['GET'],
        ))
        session.mount('http://', adapter)
        session.mount('https://', adapter)
        return session

    def fetch(self, url: str, params: dict = None, timeout: int = None):
        """GET-запрос к API с соблюдением паузы между запросами."""
        wait = self.min_interval - (time.monotonic() - self._last_request)
        if wait > 0:
            time.sleep(wait)
        try:
            response = self.session.get(url, params=params, timeout=timeout or self.api_timeout)
        finally:
            self._last_request = time.monotonic()
        response.raise_for_status()
        return response

    @abstractmethod
    def crawl_for_docs(self, year: int) -> Iterator[DocumentRecord]:
        """Обходит источник и возвращает копии документов, опубликованных в указанном году."""

    def iter_docs(self, year: int, limit: Optional[int] = None) -> Iterator[DocumentRecord]:
        """
        Возвращает отфильтрованный поток записей: только те, у которых есть
        прямая ссылка на PDF или страница документа.
        """
        count = 0
        for record in self.crawl_for_docs(year):
            if limit and count >= limit:
                break
            if not (record.pdf_url or record.landing_url):
                continue
            count += 1
            yield record

    def save_records(self, records: Iterable[DocumentRecord], batch_size: Optional[int] = None) -> HarvestStats:
        """
        Сохраняет записи в БД батчами с upsert. Возвращает статистику.

        Args:
            records: итерируемый объект DocumentRecord
            batch_size: размер партии (по умолчанию self.batch_size)
        """
        if self.db_session is None:
            raise RuntimeError('Для сохранения нужна сессия БД')

        batch_size = batch_size or self.batch_size
        stats = HarvestStats()
        batch: List[DocumentRecord] = []

        for record in records:
            stats.found += 1
            batch.append(record)
            if len(batch) >= batch_size:
                self._flush_batch(batch, stats)
                batch = []

        if batch:
            self._flush_batch(batch, stats)

        logger.info(
            f'[{self.source_name}] найдено {stats.found}: новых работ {stats.new_works}, '
            f'новых копий {stats.new_links}, уже было в БД {stats.known}'
        )
        return stats

    def harvest(self, year: int, limit: Optional[int] = None, batch_size: Optional[int] = None) -> HarvestStats:
        """
        Полный цикл: обход источника и сохранение найденных копий в БД.

        Args:
            year: год публикации
            limit: сколько новых копий сохранить (None - без ограничения)
            batch_size: размер партии сохранения
        """
        records = self.iter_docs(year, limit)
        return self.save_records(records, batch_size)

    # ---------- внутреннее сохранение ----------

    def _flush_batch(self, batch: List[DocumentRecord], stats: HarvestStats) -> None:
        """Сохраняет одну партию записей в одной транзакции."""
        for record in batch:
            result = self._save_record(record)
            if result == 'work':
                stats.new_works += 1
            elif result == 'link':
                stats.new_links += 1
            else:
                stats.known += 1
        self.db_session.commit()

    def _save_record(self, record: DocumentRecord) -> Optional[str]:
        """
        Сохраняет одну запись. Возвращает 'work' (новая работа), 'link' (новая копия
        известной работы) или None (копия уже была в БД либо ошибка).

        При существующей WorkSource выполняется upsert: обновляются pdf_url,
        landing_url и сбрасывается статус ошибки.
        """
        link_id = WorkSource.generate_id(self.source_name, record.external_id)
        db = self.db_session

        existing = db.get(WorkSource, link_id)
        if existing:
            existing.pdf_url = (record.pdf_url or '')[:1000] or None
            existing.landing_url = (record.landing_url or '')[:1000] or None
            existing.status = WorkSource.STATUS_NEW
            existing.error = None
            return None

        try:
            work = (
                db.query(Work).filter_by(doi=record.doi).first()
                if record.doi else
                None
            )
            if work is None:
                work = Work(
                    doi=record.doi,
                    title=(record.title or '')[:2000] or None,
                    year=record.year,
                )
                db.add(work)
                db.flush()
                result = 'work'
            else:
                result = 'link'

            db.add(WorkSource(
                id=link_id,
                work_id=work.id,
                source=self.source_name,
                external_id=record.external_id[:255],
                pdf_url=(record.pdf_url or '')[:1000] or None,
                landing_url=(record.landing_url or '')[:1000] or None,
            ))
            return result
        except Exception as e:
            db.rollback()
            logger.error(f'[{self.source_name}] Ошибка при сохранении {record.external_id}: {e}')
            return None

    def close(self):
        """Закрывает HTTP-сессию. Внешняя db_session не закрывается."""
        self.session.close()
