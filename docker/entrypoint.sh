#!/bin/bash
set -e

# cron запускает задания с пустым окружением - сохраняем переменные для них
printenv | grep -E '^(DATABASE_URL|CONTACT_EMAIL|DIRECT_HOSTS|CROSSREF_MEMBERS_CACHE|APP_ENV)=' | sed 's/^/export /' > /app/.env.cron

# Если передан аргумент, выполняем его (ручной запуск, например python run.py --limit 100)
if [ $# -gt 0 ]; then
    exec "$@"
fi

# cron и запуск по расписанию ЗАКОММЕНТИРОВАНЫ до перехода на продакшн.
# Раскомментировать при готовности к продакшен-режиму:
# touch /var/log/cron.log
# cron
# exec tail -f /var/log/cron.log

# При разработке контейнер запускается без cron (ручной запуск команд через docker compose exec)
exec tail -f /dev/null
