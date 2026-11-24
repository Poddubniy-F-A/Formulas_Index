"""HTTP-сессия с маршрутизацией по домену.

Российские сайты (.ru, .su, .рф) открываются напрямую, остальные — через
системный прокси. Маршрут пересчитывается на каждом редиректе.
"""

import logging
import os
import re
import urllib.request
from urllib.parse import urlparse

import requests

logger = logging.getLogger(__name__)

USER_AGENT = 'formula-index/0.2'  # слово 'harvester' в имени блокирует платформу Elpub

DIRECT_HOSTS = re.compile(
    os.getenv('DIRECT_HOSTS') or r'(\.|^)(ru|su|xn--p1ai)$', re.IGNORECASE
)


class RoutedSession(requests.Session):
    """Сессия, выбирающая маршрут по домену."""

    def __init__(self):
        super().__init__()
        self.trust_env = False
        self.system_proxies = {
            k: v for k, v in urllib.request.getproxies().items() if k in ('http', 'https')
        }
        self.headers['User-Agent'] = USER_AGENT

    def select_proxies(self, url: str) -> dict:
        """Вернуть прокси для URL: {} для российских доменов, иначе системные."""
        host = urlparse(url).hostname or ''
        return {} if DIRECT_HOSTS.search(host) else dict(self.system_proxies)

    def request(self, method, url, **kwargs):
        if 'proxies' in kwargs:
            return super().request(method, url, **kwargs)
        proxies = self.select_proxies(url)
        try:
            return super().request(method, url, proxies=proxies, **kwargs)
        except (requests.ConnectionError, requests.Timeout):
            # российские сайты встречаются и вне зон .ru/.рф (.com, .org):
            # при сбое соединения запрос повторяется по второму маршруту
            alternative = {} if proxies else dict(self.system_proxies)
            if alternative == proxies:
                raise
            return super().request(method, url, proxies=alternative, **kwargs)

    def select_proxies_for_redirect(self, prepared_request, proxies):
        """Маршрут выбирается заново для каждого редиректа."""
        return self.select_proxies(prepared_request.url)
