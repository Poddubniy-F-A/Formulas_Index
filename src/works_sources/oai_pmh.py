"""Обходчик российских журналов и репозиториев по протоколу OAI-PMH."""

import logging
import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from dataclasses import dataclass
from typing import Iterator, List, Optional

from src.base_harvester import BaseHarvester, DocumentRecord
from src.db import HarvestState
from src.pdf_resolver import looks_like_pdf_url

logger = logging.getLogger(__name__)

NS = {
    'oai': 'http://www.openarchives.org/OAI/2.0/',
    'oai_dc': 'http://www.openarchives.org/OAI/2.0/oai_dc/',
    'dc': 'http://purl.org/dc/elements/1.1/',
}

DOI_PATTERN = re.compile(r'10\.\d{4,9}/[^\s"<>]+')
PLAIN_DATE = re.compile(r'^\d{4}(-\d{2}){0,2}$')
INVALID_XML_CHARS = re.compile(r'[\x00-\x08\x0b\x0c\x0e-\x1f]')


@dataclass
class OaiEndpoint:
    name: str  # короткое имя, входит в external_id
    url: str  # адрес OAI-PMH
    set_spec: Optional[str] = None  # набор записей (журнал, коллекция)
    subject_pattern: Optional[str] = None  # фильтр по dc:subject/dc:title для многопрофильных репозиториев


# Репозитории российских университетов (DSpace). Перечень составлен по спискам
# репозиториев OpenAlex и поставщиков данных CORE; журналы с OAI-PMH (OJS, Elpub)
# сюда не входят - их статьи приходят через Crossref и DOAJ.
# Новый репозиторий добавляется одной строкой.
DEFAULT_ENDPOINTS: List[OaiEndpoint] = [
    OaiEndpoint('sfu', 'https://elib.sfu-kras.ru/oai/request'),  # СФУ
    OaiEndpoint('urfu', 'https://elar.urfu.ru/oai/request'),  # УрФУ
    OaiEndpoint('usma', 'https://elib.usma.ru/oai/request'),  # УГМУ
    OaiEndpoint('rsvpu', 'https://elar.rsvpu.ru/oai/request'),  # РГППУ
    OaiEndpoint('uspu', 'https://elar.uspu.ru/oai/request'),  # УрГПУ
    # отключены (проверить позже):
    # OaiEndpoint('samara', 'https://repo.ssau.ru/oai/request'),  # Самарский ун-т: в записях внутренний адрес сервера
    # OaiEndpoint('spbu', 'https://dspace.spbu.ru/oai/request'),  # СПбГУ: 403 на ListRecords
    # OaiEndpoint('susu', 'https://dspace.susu.ru/oai/request'),  # ЮУрГУ: 500 на ListRecords
    # OaiEndpoint('nsu', 'https://dspace.nsu.ru/oai/request'),  # НГУ: 403 на ListRecords
    # OaiEndpoint('usfeu', 'https://elar.usfeu.ru/oai/request'),  # УрГЛТУ: 502
    # OaiEndpoint('belinsky', 'https://elib.uraic.ru/oai/request'),  # библиотека им. Белинского: таймаут
]


class OaiPmhHarvester(BaseHarvester):
    """
    Обходит репозитории российских университетов по OAI-PMH: диссертации, выпускные
    работы, препринты, сборники - обычно без DOI и без копий в других источниках.
    В записях Dublin Core обычно есть ссылка на страницу документа (dc:identifier);
    PDF на ней находится на этапе загрузки (мета-тег citation_pdf_url).

    Сбор инкрементальный, как принято для OAI-PMH: первый обход забирает весь каталог
    репозитория, следующие - только записи, изменённые с прошлого успешного обхода
    (дата хранится в таблице harvest_state). Год публикации здесь не используется:
    репозитории массово обновляют старые записи, и отбор по году потребовал бы
    просматривать почти весь каталог. Ошибка одного репозитория не останавливает
    обход остальных.
    """

    min_interval = 1.0

    def __init__(self, db_session=None, endpoints: List[OaiEndpoint] = None,
                 per_endpoint_limit: int = None, **kwargs):
        """
        Args:
            endpoints: список репозиториев (по умолчанию DEFAULT_ENDPOINTS)
            per_endpoint_limit: не больше N записей из каждого репозитория (для тестов;
                                дата успешного обхода при этом не сохраняется)
        """
        super().__init__('oai', 'oai-pmh', db_session, **kwargs)
        self.endpoints = endpoints or DEFAULT_ENDPOINTS
        self.per_endpoint_limit = per_endpoint_limit

    @staticmethod
    def extract_publication_year(dates: List[str]) -> Optional[int]:
        # dc:date содержит и служебные даты загрузки (2007-01-14T11:00:55Z), и дату издания (2005)
        plain = [d for d in dates if PLAIN_DATE.match(d)] or dates
        years = [int(d[:4]) for d in plain if d[:4].isdigit()]
        return min(years) if years else None

    def parse_record(self, endpoint: OaiEndpoint, record: ET.Element) -> Optional[DocumentRecord]:
        header = record.find('oai:header', NS)
        if header is None or header.get('status') == 'deleted':
            return None
        dc = record.find('oai:metadata/oai_dc:dc', NS)
        if dc is None:
            return None

        def values(tag):
            return [(el.text or '').strip() for el in dc.findall(f'dc:{tag}', NS) if el.text]

        titles = values('title')
        if endpoint.subject_pattern:
            text = ' '.join(values('subject') + titles)
            if not re.search(endpoint.subject_pattern, text, re.IGNORECASE):
                return None

        identifiers = values('identifier') + values('relation')
        doi = next((m.group(0) for i in identifiers for m in [DOI_PATTERN.search(i)] if m), None)
        urls = [u for u in identifiers if u.startswith('http')]
        pdf_url = next((u for u in urls if looks_like_pdf_url(u)), None)
        landing_url = next((u for u in urls if u != pdf_url and not DOI_PATTERN.search(u)), None)
        if not (pdf_url or landing_url):
            return None

        return DocumentRecord(
            external_id=f"{endpoint.name}:{header.findtext('oai:identifier', '', NS)}",
            pdf_url=pdf_url,
            landing_url=landing_url,
            title=titles[0] if titles else None,
            doi=doi,
            year=self.extract_publication_year(values('date')),
        )

    def stream_endpoint_records(self, endpoint: OaiEndpoint, since: Optional[str]) -> Iterator[DocumentRecord]:
        params = {'verb': 'ListRecords', 'metadataPrefix': 'oai_dc'}
        if since:
            params['from'] = since
        if endpoint.set_spec:
            params['set'] = endpoint.set_spec
        while True:
            # часть источников отдаёт недопустимые в XML управляющие символы
            content = self.fetch(endpoint.url, params=params, timeout=60).content.decode('utf-8', errors='replace')
            root = ET.fromstring(INVALID_XML_CHARS.sub('', content))
            error = root.find('oai:error', NS)
            if error is not None:
                if error.get('src') != 'noRecordsMatch':
                    raise RuntimeError(f"ошибка OAI-PMH {error.get('src')}: {error.text}")
                return
            for record in root.iterfind('oai:ListRecords/oai:record', NS):
                document = self.parse_record(endpoint, record)
                if document:
                    yield document
            token = root.findtext('oai:ListRecords/oai:resumptionToken', '', NS).strip()
            if not token:
                return
            params = {'verb': 'ListRecords', 'resumptionToken': token}

    def load_harvest_state(self, endpoint: OaiEndpoint) -> HarvestState:
        key = f'oai:{endpoint.name}'
        state = self.db_session.get(HarvestState, key) if self.db_session else None
        return state or HarvestState(key=key)

    def crawl_for_docs(self, year: int) -> Iterator[DocumentRecord]:
        """
        Год не используется: репозитории собираются инкрементально (см. описание класса).
        Параметр year оставлен для соответствия абстрактному интерфейсу BaseHarvester.
        """
        for endpoint in self.endpoints:
            state = self.load_harvest_state(endpoint)
            started = (datetime.utcnow() - timedelta(days=1)).strftime('%Y-%m-%d')
            found, complete = 0, False
            try:
                for document in self.stream_endpoint_records(endpoint, state.last_datestamp):
                    try:
                        yield document
                    except Exception as e:
                        logger.warning(f'[oai:{endpoint.name}] пропуск записи: {e}')
                    found += 1
                    if self.per_endpoint_limit and found >= self.per_endpoint_limit:
                        break
                else:
                    complete = True
            except Exception as e:
                logger.error(f"[oai:{endpoint.name}] источник пропущен: {type(e).__name__}: {e}")
            since = f"изменённых с {state.last_datestamp}" if state.last_datestamp else "весь каталог"
            logger.info(f"[oai:{endpoint.name}] записей ({since}): {found}{'' if complete else ', обход не завершён'}")
            if complete and self.db_session is not None:
                state.last_datestamp = started
                self.db_session.merge(state)
                self.db_session.commit()
