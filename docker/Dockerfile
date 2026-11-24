FROM python:3.11-slim

RUN apt-get update && apt-get install -y \
    cron \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

# Копируем только необходимое для работы контейнера
COPY src/ ./src/
COPY run.py ./
COPY requirements.txt ./

RUN pip install --no-cache-dir -r requirements.txt

# Скрипт резервного копирования БД (внутри образа, не монтируется)
COPY docker/database/backup.sh /backup.sh
RUN chmod +x /backup.sh

COPY docker/entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENTRYPOINT ["/entrypoint.sh"]
