"""Этап извлечения формул и построения индекса.

Для каждого скачанного документа:
    MinerU (отдельное окружение, подпроцесс) -> формулы LaTeX с номерами страниц
    -> разбор в деревья (latex_parser) -> formulas / formula_occurrences в БД
    + новые формулы в дерево подстановок (файл) -> Downloader.release (в продакшн-версии удаляет PDF).

Все изменения в БД для одного документа выполняются в одной транзакции:
либо все формулы и вхождения попадают в БД, либо при ошибке откат (rollback).
Файл дерева подстановок обновляется только после успешного коммита.
"""

import json
import logging
import os
import subprocess
import sys
import tempfile
import time
from collections import Counter
from dataclasses import dataclass
from typing import List, Optional

sys.setrecursionlimit(100000)

from src.db import WorkFile, Formula, FormulaOccurrence
from src.downloader import Downloader
from src.formula_index.latex_parser import canonical, parse_latex
from src.formula_index.tree_index import SubstitutionTree

logger = logging.getLogger(__name__)

RUNNER = os.path.join(os.path.dirname(os.path.abspath(__file__)), 'mineru_runner.py')

# Все PDF индексируются постранично, чтобы не перегружать память MinerU.


def default_mineru_python() -> str:
    """Интерпретатор окружения MinerU: переменная MINERU_PYTHON, затем ~/.mineru/venv, затем системный python."""
    if os.getenv('MINERU_PYTHON'):
        return os.environ['MINERU_PYTHON']
    venv = os.path.join(os.path.expanduser('~'), '.mineru', 'venv')
    venv_python = os.path.join(venv, 'Scripts', 'python.exe') if os.name == 'nt' else os.path.join(venv, 'bin', 'python')
    if os.path.exists(venv_python):
        return venv_python
    import sys
    return sys.executable


@dataclass
class RawFormula:
    page: int
    latex: str
    display: bool


def extract_formulas(pdf_path: str, tier: str = 'basic', python: Optional[str] = None,
                     timeout: int = 3600) -> List[RawFormula]:
    """Распознаёт PDF через MinerU и возвращает найденные формулы."""
    fd, out_path = tempfile.mkstemp(suffix='.json')
    os.close(fd)
    try:
        env = dict(os.environ, PYTHONIOENCODING='utf-8', PYTHONWARNINGS='ignore')
        result = subprocess.run([python or default_mineru_python(), RUNNER, os.path.abspath(pdf_path), out_path, tier],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=timeout, env=env)
        if result.returncode != 0:
            tail = (result.stderr or result.stdout).strip().splitlines()[-3:]
            raise RuntimeError(f'MinerU завершился с кодом {result.returncode}: {" | ".join(tail)}')
        with open(out_path, encoding='utf-8') as f:
            data = json.load(f)
        return [RawFormula(**item) for item in data['formulas']]
    finally:
        os.remove(out_path)


def extract_formulas_by_pages(pdf_path: str, tier: str = 'basic', python: Optional[str] = None,
                              timeout: int = 3600) -> List[RawFormula]:
    """Распознаёт PDF постранично, если файл слишком велик для одного вызова MinerU.

    Формулы не переносятся через границы страниц, поэтому разбиение по страницам
    безопасно для целостности формул. Каждая страница конвертируется во временный
    односторонний PDF через pypdf и передаётся в MinerU отдельно.
    """
    from pypdf import PdfReader, PdfWriter

    formulas: List[RawFormula] = []
    reader = PdfReader(pdf_path)
    total_pages = len(reader.pages)

    for page_idx in range(total_pages):
        fd, page_path = tempfile.mkstemp(suffix='.pdf')
        os.close(fd)
        try:
            writer = PdfWriter()
            writer.add_page(reader.pages[page_idx])
            with open(page_path, 'wb') as f:
                writer.write(f)
            page_formulas = extract_formulas(page_path, tier=tier, python=python, timeout=timeout)
            # корректируем номера страниц на общие
            for f in page_formulas:
                f.page = page_idx + 1
            formulas.extend(page_formulas)
            logger.info(f'Страница {page_idx + 1}/{total_pages}: найдено формул {len(page_formulas)}')
        except RecursionError as e:
            logger.warning(f'Страница {page_idx + 1}/{total_pages}: рекурсия ({e}), пропускаем страницу')
        except Exception as e:
            logger.warning(f'Страница {page_idx + 1}/{total_pages}: ошибка ({type(e).__name__}: {e}), пропускаем страницу')
        finally:
            os.remove(page_path)

    return formulas


@dataclass
class IndexStats:
    documents: int = 0
    failed: int = 0
    formulas: int = 0  # формул LaTeX, найденных MinerU
    parsed: int = 0  # деревьев разбора (формула может делиться на части по , ; &)
    new_formulas: int = 0  # новых уникальных формул в индексе


class Indexer:
    def __init__(self, db_session, downloader: Downloader, tree_path: str, tier: str = 'basic',
                 mineru_python: Optional[str] = None):
        """
        Args:
            tree_path: файл дерева подстановок (читается веб-приложением при поиске)
            tier: модели MinerU - basic (быстрее, CPU) или standard (+ VLM)
        """
        self.db = db_session
        self.downloader = downloader
        self.tree = SubstitutionTree(tree_path)
        self.tier = tier
        self.python = mineru_python

    def index_document(self, document: WorkFile, stats: IndexStats):
        """Индексирует один документ транзакционно.

        Все формулы и вхождения для документа загружаются в БД одной транзакцией:
        либо всё.commit(), либо полный rollback. Файл дерева подстановок
        обновляется только после успешного коммита.
        """
        pdf_path = self.downloader.local_path(document.file_path)

        # Всегда индексируем постранично, чтобы не перегружать память MinerU
        logger.info(f'{document.sha256[:12]}: индексация постранично')
        raw = extract_formulas_by_pages(pdf_path, self.tier, self.python)

        stats.formulas += len(raw)

        tf = Counter()  # (id формулы, страница) -> число вхождений
        found = {}  # id -> (дерево, LaTeX)
        for formula in raw:
            for parsed in parse_latex(formula.latex):
                tf[(parsed.id, formula.page)] += 1
                found.setdefault(parsed.id, (parsed.tree, formula.latex))
        stats.parsed += sum(tf.values())

        # Собираем все объекты в памяти перед записью в БД
        new_formula_objs: List[Formula] = []
        tree_entries: List[tuple] = []  # (tree, formula_id) для новых формул
        occurrences: List[FormulaOccurrence] = []

        for formula_id, (tree, latex) in found.items():
            formula = self.db.get(Formula, formula_id)
            if formula is None:
                formula = Formula(id=formula_id, tree=canonical(tree), latex=latex, df=0)
                new_formula_objs.append(formula)
                tree_entries.append((tree, formula_id))
                stats.new_formulas += 1
            formula.df += 1

        for (formula_id, page), count in tf.items():
            occurrences.append(FormulaOccurrence(
                formula_id=formula_id,
                pdf_sha256=document.sha256,
                page=page,
                tf=count,
            ))

        # Удаляем старые вхождения документа (повторная индексация того же PDF)
        self.db.query(FormulaOccurrence).filter(
            FormulaOccurrence.pdf_sha256 == document.sha256
        ).delete(synchronize_session=False)

        # Одна транзакция: либо все формулы и вхождения попадают в БД, либо rollback
        try:
            self.db.add_all(new_formula_objs)
            self.db.add_all(occurrences)
            self.db.flush()
            self.db.commit()
        except Exception:
            self.db.rollback()
            raise

        # Только после успешного коммита обновляем файл дерева подстановок
        for tree, formula_id in tree_entries:
            self.tree.add(tree, formula_id)
        self.tree.save()

        self.downloader.release(document, indexed=True)  # помечает is_indexed=True + commit

    def run(self, limit: Optional[int] = None) -> IndexStats:
        stats = IndexStats()
        for document in self.downloader.documents_to_index(limit):
            started = time.time()
            try:
                self.index_document(document, stats)
                stats.documents += 1
                logger.info(f'{document.sha256[:12]}: готово за {time.time() - started:.0f} с; '
                            f'всего формул {stats.formulas}, новых в индексе {stats.new_formulas}')
            except RecursionError as e:
                self.db.rollback()
                stats.failed += 1
                logger.error(f'{document.sha256[:12]}: RecursionError (глубокая вложенность структуры MinerU): {e}. '
                             f'Документ пропущен.')
                self.downloader.release(document, indexed=True)
            except Exception as e:
                self.db.rollback()
                stats.failed += 1
                logger.error(f'{document.sha256[:12]}: {type(e).__name__}: {e}')
                self.downloader.release(document, indexed=True)
        logger.info(f'Извлечение завершено. Документов {stats.documents}, ошибок {stats.failed}; '
                    f'формул {stats.formulas}, деревьев {stats.parsed}, новых формул {stats.new_formulas}')
        return stats
