#!/bin/sh
# Резервная копия БД: выгружает formula_index в /backups (каталог backups/ проекта - вне Docker)
# и хранит BACKUP_KEEP последних копий. По умолчанию делает одну копию и завершается;
# при BACKUP_INTERVAL > 0 повторяет выгрузку с этим периодом (секунд).
# Восстановление: см. README, раздел "Резервные копии".

KEEP=${BACKUP_KEEP:-14}
INTERVAL=${BACKUP_INTERVAL:-0}

while true; do
    file=/backups/formula_index_$(date +%Y-%m-%d_%H%M).dump
    if pg_dump -h postgres -U python -d formula_index -Fc -f "$file.tmp"; then
        mv "$file.tmp" "$file"
        echo "$(date '+%F %T') копия сохранена: $file"
    else
        rm -f "$file.tmp"
        echo "$(date '+%F %T') ошибка резервного копирования"
        [ "$INTERVAL" -gt 0 ] || exit 1
    fi
    ls -1t /backups/formula_index_*.dump 2>/dev/null | tail -n +$((KEEP + 1)) | xargs -r rm -f
    [ "$INTERVAL" -gt 0 ] || exit 0
    sleep "$INTERVAL"
done
