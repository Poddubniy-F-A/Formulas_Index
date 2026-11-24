"""Этап загрузки: поиск, скачивание и проверка PDF для собранных работ.

Для каждой работы, у которой ещё нет документа, перебираются её копии (work_sources):
сначала прямые ссылки на PDF, затем страницы документов (PDF ищется по мета-тегу
citation_pdf_url). Первый успешно скачанный PDF сохраняется во временное хранилище,
по SHA-256 содержимого определяется документ (pdf_files). Одинаковые файлы, полученные
разными путями, дают один документ.

После извлечения формул этап извлечения вызывает release(). В продакшн-версии
(APP_ENV=production) файл при этом удаляется; при разработке PDF сохраняются, чтобы
документы можно было просматривать и индексировать повторно.
"""

import hashlib
import logging
import os
import tempfile
from dataclasses import dataclass
from datetime import datetime
from typing import Iterator, List, Optional, Tuple

from sqlalchemy import exists, or_, and_

from src.session import RoutedSession
from src.db import WorkFile, WorkSource, Work
from src.pdf_resolver import normalize_pdf_url, resolve_pdf_url

logger = logging.getLogger(__name__)

PRODUCTION = os.getenv('APP_ENV', 'development').lower() == 'production'

SOURCE_PRIORITY = {'doaj': 0, 'oai': 1, 'crossref': 2}  # чьи прямые ссылки пробовать раньше


@dataclass
class DownloadStats:
    works: int = 0  # обработано работ
    downloaded: int = 0  # получен новый документ
    same_file: int = 0  # PDF совпал с уже известным документом (по SHA-256)
    failed: int = 0  # PDF не получен ни по одной копии


class Downloader:
    def __init__(self, db_session, storage_dir: str = 'pdf_storage', timeout: int = 15,
                 max_size_mb: int = 50, all_copies: bool = False, delete_indexed: bool = PRODUCTION,
                 retry_unavailable: bool = False):
        """
        Args:
            storage_dir: каталог временного хранения PDF до извлечения формул
            timeout: таймаут одного HTTP-запроса, секунд
            max_size_mb: файлы больше этого размера не скачиваются
            all_copies: скачивать все копии работы, а не только первую доступную
                        (разные PDF одной работы станут разными документами)
            delete_indexed: удалять PDF после извлечения формул (по умолчанию - только при APP_ENV=production)
            retry_unavailable: повторно проверять копии со статусом STATUS_UNAVAILABLE
                               (контент на сайте мог появиться с первого неудачного attempts)
        """
        self.db = db_session
        self.storage_dir = storage_dir
        self.timeout = timeout
        self.max_size = max_size_mb * 1024 * 1024
        self.all_copies = all_copies
        self.delete_indexed = delete_indexed
        self.retry_unavailable = retry_unavailable
        self.session = RoutedSession()
        os.makedirs(storage_dir, exist_ok=True)

    # ---------- выбор работ ----------

    def pending_works(self, limit: Optional[int]) -> List[Work]:
        """Работы, у которых нет документа и остались непроверенные копии.

        По умолчанию берутся только копии со статусом STATUS_NEW.
        При retry_unavailable=True дополнительно включаются копии со статусом
        STATUS_UNAVAILABLE, но только те, которые были проверены ДО начала
        этого запуска (checked_at < run_started_at) — нет смысла повторно
        "дергать" только что добавленные ссылки.
        """
        if self.retry_unavailable:
            status_condition = or_(
                WorkSource.status == WorkSource.STATUS_NEW,
                and_(
                    WorkSource.status == WorkSource.STATUS_UNAVAILABLE,
                    WorkSource.checked_at.is_not(None),
                    WorkSource.checked_at < self._run_started_at,
                ),
            )
        else:
            status_condition = WorkSource.status == WorkSource.STATUS_NEW

        query = (self.db.query(Work)
                 .filter(~exists().where(WorkFile.work_id == Work.id))
                 .filter(exists().where((WorkSource.work_id == Work.id) &
                                        status_condition))
                 .order_by(Work.id))
        return query.limit(limit).all() if limit else query.all()

    @staticmethod
    def _ordered_links(work: Work) -> List[WorkSource]:
        links = [link for link in work.sources if link.status == WorkSource.STATUS_NEW]
        # сначала копии с прямой ссылкой на PDF
        return sorted(links, key=lambda l: (l.pdf_url is None, SOURCE_PRIORITY.get(l.source, 9)))

    # ---------- загрузка ----------

    def _candidates(self, link: WorkSource) -> Iterator[str]:
        """Ссылки на PDF для копии: прямая ссылка, затем PDF со страницы документа."""
        tried = set()
        if link.pdf_url:
            tried.add(link.pdf_url)
            yield link.pdf_url
        if link.landing_url:
            pdf = resolve_pdf_url(self.session, link.landing_url, timeout=self.timeout)
            pdf = normalize_pdf_url(pdf)
            if pdf and pdf not in tried:
                yield pdf

    def _download(self, url: str) -> Tuple[Optional[str], Optional[str], int]:
        """
        Скачивает PDF во временный файл. Возвращает (путь, sha256, размер)
        или (None, причина, 0), если по ссылке не PDF.
        """
        try:
            with self.session.get(url, timeout=self.timeout, stream=True, allow_redirects=True) as response:
                if not response.ok:
                    return None, f'HTTP {response.status_code}', 0
                length = int(response.headers.get('Content-Length') or 0)
                if length > self.max_size:
                    return None, f'файл больше {self.max_size // 2**20} МБ', 0
                digest, size = hashlib.sha256(), 0
                fd, path = tempfile.mkstemp(suffix='.pdf', dir=self.storage_dir)
                with os.fdopen(fd, 'wb') as f:
                    for i, chunk in enumerate(response.iter_content(chunk_size=65536)):
                        if i == 0 and not chunk.lstrip().startswith(b'%PDF'):
                            f.close()
                            os.remove(path)
                            kind = response.headers.get('Content-Type', '?').split(';')[0]
                            return None, f'не PDF ({kind})', 0
                        size += len(chunk)
                        if size > self.max_size:
                            f.close()
                            os.remove(path)
                            return None, f'файл больше {self.max_size // 2**20} МБ', 0
                        digest.update(chunk)
                        f.write(chunk)
                if size == 0:
                    os.remove(path)
                    return None, 'пустой ответ', 0
                return path, digest.hexdigest(), size
        except Exception as e:
            return None, f'{type(e).__name__}', 0

    def _store(self, work: Work, link: WorkSource, url: str, path: str, sha256: str, size: int) -> bool:
        """Регистрирует файл как документ. Возвращает True, если документ новый."""
        existing = self.db.get(WorkFile, sha256)
        if existing:
            os.remove(path)
        else:
            # путь относительно хранилища: хранилище смонтировано по-разному на хосте и в контейнере
            relative_path = f'{sha256[:2]}/{sha256}.pdf'
            final_path = self.local_path(relative_path)
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
            os.replace(path, final_path)
            self.db.add(WorkFile(sha256=sha256, work_id=work.id, size=size, pdf_url=url[:1000],
                                 file_path=relative_path))
        link.status = WorkSource.STATUS_OK
        link.pdf_sha256 = sha256
        link.error = None
        link.checked_at = datetime.utcnow()
        return existing is None

    def process_work(self, work: Work, stats: DownloadStats):
        got_any = False
        links = self._ordered_links(work)
        for link in links:
            if got_any and not self.all_copies:
                link.status = WorkSource.STATUS_SKIPPED
                link.checked_at = datetime.utcnow()
                continue
            errors = []
            for url in self._candidates(link):
                path, sha_or_error, size = self._download(url)
                if path:
                    if self._store(work, link, url, path, sha_or_error, size):
                        stats.downloaded += 1
                    else:
                        stats.same_file += 1
                    got_any = True
                    break
                errors.append(f'{url[:120]}: {sha_or_error}')
            else:
                link.status = WorkSource.STATUS_UNAVAILABLE
                link.error = '; '.join(errors) or 'PDF не найден на странице документа'
                link.checked_at = datetime.utcnow()
        if not got_any:
            stats.failed += 1
        self.db.commit()

    def run(self, limit: Optional[int] = None) -> DownloadStats:
        stats = DownloadStats()
        self._run_started_at = datetime.utcnow()
        for work in self.pending_works(limit):
            stats.works += 1
            try:
                self.process_work(work, stats)
            except Exception as e:
                self.db.rollback()
                logger.error(f"Работа {work.id}: {type(e).__name__}: {e}")
            if stats.works % 25 == 0:
                logger.info(f"обработано работ {stats.works}: новых документов {stats.downloaded}, "
                            f"совпало с известными {stats.same_file}, без PDF {stats.failed}")
        logger.info(f"Загрузка завершена. Работ {stats.works}: новых документов {stats.downloaded}, "
                    f"совпало с известными {stats.same_file}, без PDF {stats.failed}")
        return stats

    # ---------- для этапа извлечения формул ----------

    def local_path(self, file_path: str) -> str:
        """Путь к файлу документа в хранилище."""
        return os.path.join(self.storage_dir, *file_path.split('/'))

    def documents_to_index(self, limit: Optional[int] = None) -> List[WorkFile]:
        """Скачанные, но ещё не проиндексированные документы."""
        query = (self.db.query(WorkFile).filter(WorkFile.is_indexed.is_(False), WorkFile.file_path.isnot(None))
                 .order_by(WorkFile.downloaded_at))
        return query.limit(limit).all() if limit else query.all()

    def release(self, document: WorkFile, indexed: bool = True):
        """Отмечает документ после извлечения формул; в продакшн-версии удаляет его файл."""
        if self.delete_indexed:
            path = document.file_path and self.local_path(document.file_path)
            if path and os.path.exists(path):
                os.remove(path)
            document.file_path = None
        if indexed:
            document.is_indexed = True
            document.indexed_at = datetime.utcnow()
        self.db.commit()
