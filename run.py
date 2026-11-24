"""Пайплайн подготовки документов для извлечения формул.

Этапы:
    harvest   - сбор метаданных и ссылок из источников (Crossref, DOAJ, OAI-PMH)
    download  - поиск и скачивание PDF, проверка, SHA-256 (временное хранилище)
    extract   - извлечение формул (MinerU), разбор, запись в индекс (PDF удаляется при APP_ENV=production)
    stats     - сводка по БД

Примеры:
    python run.py harvest --year 2024 --limit 100
    python run.py harvest --works_sources doaj oai --year 2024
    python run.py download --limit 300
    python run.py extract --limit 5
    python run.py stats
"""

import argparse
import logging
from datetime import datetime

from sqlalchemy import func

from src.formula_index.indexer import Indexer
from src.db import WorkFile, WorkSource, Formula, FormulaOccurrence, Work, get_db_session
from src.downloader import Downloader
from src.works_sources import HARVESTERS

logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(name)s - %(levelname)s - %(message)s')
logging.getLogger('urllib3').setLevel(logging.ERROR)
logger = logging.getLogger(__name__)


def harvest(args):
    logger.info(f"Сбор за {args.year} год: {', '.join(args.works_sources)}")
    for name in args.works_sources:
        options = {'per_endpoint_limit': args.oai_per_repo} if name == 'oai' and args.oai_per_repo else {}
        harvester = HARVESTERS[name](db_session=get_db_session(args.db), **options)
        try:
            harvester.harvest(args.year, args.limit)
        except Exception as e:
            logger.error(f"Ошибка в обходчике {name}: {type(e).__name__}: {e}")
        finally:
            harvester.close()


def download(args):
    downloader = Downloader(get_db_session(args.db), storage_dir=args.storage, timeout=args.timeout,
                            max_size_mb=args.max_size, all_copies=args.all_copies,
                            retry_unavailable=args.retry_unavailable)
    downloader.run(args.limit)


def extract(args):
    db = get_db_session(args.db)
    indexer = Indexer(db, Downloader(db, storage_dir=args.storage), tree_path=args.tree, tier=args.tier,
                      mineru_python=args.mineru_python)
    indexer.run(args.limit)


def stats(args):
    db = get_db_session(args.db)
    print(f"Работ: {db.query(Work).count()}, из них с DOI: {db.query(Work).filter(Work.doi.isnot(None)).count()}")
    print(f"Документов (уникальных PDF): {db.query(WorkFile).count()}, "
          f"ожидают извлечения формул: {db.query(WorkFile).filter(WorkFile.is_indexed.is_(False), WorkFile.file_path.isnot(None)).count()}")
    print(f"Проиндексировано документов: {db.query(WorkFile).filter(WorkFile.is_indexed.is_(True)).count()}, "
          f"уникальных формул: {db.query(Formula).count()}, вхождений: {db.query(FormulaOccurrence).count()}")
    print("Копии по источникам и статусам:")
    rows = (db.query(WorkSource.source, WorkSource.status, func.count())
            .group_by(WorkSource.source, WorkSource.status).order_by(WorkSource.source).all())
    for source, status, count in rows:
        print(f"  {source:10} {status:12} {count}")
    shared = (db.query(Work.id).join(WorkSource).group_by(Work.id)
              .having(func.count(func.distinct(WorkSource.source)) > 1).count())
    print(f"Работ, найденных в нескольких источниках: {shared}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--db', default=None, help='адрес БД (по умолчанию DATABASE_URL или localhost:5433)')
    commands = parser.add_subparsers(dest='command', required=True)

    p = commands.add_parser('harvest', help='сбор метаданных и ссылок')
    p.add_argument('--works_sources', nargs='+', choices=HARVESTERS, default=list(HARVESTERS))
    p.add_argument('--year', type=int, default=datetime.now().year)
    p.add_argument('--limit', type=int, default=None, help='не больше N новых копий с каждого источника')
    p.add_argument('--oai-per-repo', type=int, default=None, help='не больше N записей из каждого OAI-репозитория')
    p.set_defaults(func=harvest)

    p = commands.add_parser('download', help='скачивание и проверка PDF')
    p.add_argument('--limit', type=int, default=None, help='сколько работ обработать')
    p.add_argument('--storage', default='/app/pdf_storage', help='каталог временного хранения PDF')
    p.add_argument('--timeout', type=int, default=15, help='таймаут HTTP-запроса, секунд')
    p.add_argument('--max-size', type=int, default=50, help='максимальный размер PDF, МБ')
    p.add_argument('--all-copies', action='store_true', help='скачивать все копии работы, а не первую доступную')
    p.add_argument('--retry-unavailable', action='store_true',
                   help='повторно проверять копии со статусом unavailable (контент мог появиться с первого attempts)')
    p.set_defaults(func=download)

    p = commands.add_parser('extract', help='извлечение формул и построение индекса')
    p.add_argument('--limit', type=int, default=None, help='сколько документов обработать')
    p.add_argument('--storage', default='/app/pdf_storage', help='каталог временного хранения PDF')
    p.add_argument('--tree', default='/app/index_data/formulas_tree.json', help='файл дерева подстановок')
    p.add_argument('--tier', choices=['basic', 'standard'], default='basic', help='модели MinerU')
    p.add_argument('--mineru-python', default=None, help='интерпретатор окружения MinerU '
                   '(по умолчанию MINERU_PYTHON или ~/.mineru/venv)')
    p.set_defaults(func=extract)

    p = commands.add_parser('stats', help='сводка по БД')
    p.set_defaults(func=stats)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
