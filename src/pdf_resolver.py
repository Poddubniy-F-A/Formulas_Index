"""Получение прямой ссылки на PDF по ссылке на страницу статьи.

Многие API (OpenAIRE, zbMATH) отдают не ссылку на PDF, а DOI или адрес страницы статьи.
Платформы журналов и репозиториев (OJS, Elpub, DSpace, Zenodo, arXiv) размещают на такой
странице стандартный мета-тег citation_pdf_url (формат Google Scholar), из которого
и берётся прямая ссылка.
"""

import logging
import re
from typing import Optional
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

PDF_URL_PATTERN = re.compile(r'\.pdf($|\?)|/pdf(/|$)|/download(/|$)|bitstream', re.IGNORECASE)


OJS_VIEW_PATTERN = re.compile(r'(/article/)view(/\d+/\d+)/?$')


def normalize_pdf_url(url: str) -> str:
    """
    Приводит ссылку к адресу самого файла.

    Журналы на OJS публикуют ссылку на просмотрщик PDF (.../article/view/<статья>/<файл>),
    который отдаёт HTML-страницу; сам файл доступен по .../article/download/<статья>/<файл>.
    """
    return OJS_VIEW_PATTERN.sub(r'\1download\2', url) if url else url


def looks_like_pdf_url(url: str) -> bool:
    """Эвристика: похожа ли ссылка на прямую ссылку на PDF."""
    return bool(url and PDF_URL_PATTERN.search(url))


def resolve_pdf_url(session: requests.Session, url: str, timeout: int = 15) -> Optional[str]:
    """
    Возвращает прямую ссылку на PDF для страницы статьи или None.

    1. Если по ссылке сразу отдаётся PDF - возвращается итоговый адрес (после редиректов).
    2. Иначе ищется мета-тег citation_pdf_url на HTML-странице.
    """
    try:
        response = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
        content_type = response.headers.get('Content-Type', '').lower()
        if 'application/pdf' in content_type:
            response.close()
            return response.url
        if 'html' not in content_type or not response.ok:
            response.close()
            return None
        html = response.text
    except requests.RequestException as e:
        logger.debug(f"Не удалось открыть {url}: {e}")
        return None

    meta = BeautifulSoup(html, 'html.parser').find('meta', attrs={'name': 'citation_pdf_url'})
    if meta and meta.get('content'):
        return urljoin(response.url, meta['content'].strip())
    return None


def check_pdf(session: requests.Session, url: str, timeout: int = 30) -> bool:
    """Проверяет, что по ссылке действительно доступен PDF (сигнатура %PDF в начале файла)."""
    try:
        with session.get(url, timeout=timeout, allow_redirects=True, stream=True) as response:
            if not response.ok:
                return False
            head = next(response.iter_content(chunk_size=1024), b'')
            return head.lstrip().startswith(b'%PDF')
    except requests.RequestException:
        return False
