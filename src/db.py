"""Модели БД пайплайна и подключение к БД.

Схема:
- works          - научная работа (статья, диссертация...). Копии одной работы из разных
                   источников объединяются по DOI; работа без DOI - отдельная запись.
- work_sources   - где найдена работа: каждая копия в источнике (ссылка на PDF и/или
                   страницу, результат проверки при загрузке).
- pdf_files      - уникальный PDF-файл (по SHA-256), передаваемый на извлечение формул.
                   Разные PDF одной работы - разные документы: из них могут извлекаться
                   разные формулы.
- formulas, formula_occurrences - индекс: уникальные формулы и их вхождения в документы
                   по страницам.
"""

import hashlib
import os

from sqlalchemy import (Boolean, Column, DateTime, ForeignKey, Integer, BigInteger, String, Text,
                        create_engine)
from sqlalchemy.orm import declarative_base, relationship, sessionmaker
from sqlalchemy.sql import func

Base = declarative_base()

DEFAULT_DATABASE_URL = 'postgresql://python:python@localhost:5433/formula_index'


class Work(Base):
    __tablename__ = 'works'

    id = Column(Integer, primary_key=True)
    doi = Column(String(255), unique=True)  # NULL для работ без DOI
    title = Column(Text)
    year = Column(Integer)
    created_at = Column(DateTime, server_default=func.now())

    sources = relationship('WorkSource', back_populates='work')
    pdf_files = relationship('WorkFile', back_populates='work')


class WorkSource(Base):
    __tablename__ = 'work_sources'

    STATUS_NEW = 'new'  # ещё не проверялась
    STATUS_OK = 'ok'  # PDF скачан
    STATUS_UNAVAILABLE = 'unavailable'  # PDF получить не удалось
    STATUS_SKIPPED = 'skipped'  # не проверялась: PDF работы уже получен по другой ссылке

    id = Column(String(32), primary_key=True)  # MD5 от source:external_id
    work_id = Column(Integer, ForeignKey('works.id'), nullable=False, index=True)
    source = Column(String(50), nullable=False)
    external_id  = Column(String(255), nullable=False)
    pdf_url = Column(String(1000))  # прямая ссылка на PDF, если источник её даёт
    landing_url = Column(String(1000))  # страница документа (DOI, handle, страница статьи)
    status = Column(String(20), nullable=False, default=STATUS_NEW, index=True)
    error = Column(Text)
    checked_at = Column(DateTime)
    pdf_sha256 = Column(String(64), ForeignKey('pdf_files.sha256'))
    created_at = Column(DateTime, server_default=func.now())

    work = relationship('Work', back_populates='sources')

    @staticmethod
    def generate_id(source: str, external_id: str) -> str:
        return hashlib.md5(f"{source}:{external_id}".encode()).hexdigest()


class WorkFile(Base):
    __tablename__ = 'pdf_files'

    sha256 = Column(String(64), primary_key=True)
    work_id = Column(Integer, ForeignKey('works.id'), nullable=False, index=True)
    size = Column(BigInteger)
    pdf_url = Column(String(1000))  # откуда файл был скачан
    file_path = Column(String(500))  # временный файл относительно хранилища; очищается после извлечения формул
    downloaded_at = Column(DateTime, server_default=func.now())
    is_indexed = Column(Boolean, default=False, index=True)
    indexed_at = Column(DateTime)

    work = relationship('Work', back_populates='pdf_files')


class Formula(Base):
    """Уникальная формула (по дереву разбора); само дерево поиска хранится в файле индекса."""
    __tablename__ = 'formulas'

    id = Column(String(32), primary_key=True)  # MD5 канонической записи дерева разбора
    tree = Column(Text, nullable=False)  # каноническая запись дерева (JSON)
    latex = Column(Text)  # LaTeX первого найденного вхождения - для показа в результатах
    df = Column(Integer, nullable=False, default=0)  # в скольких документах встречается


class FormulaOccurrence(Base):
    """Вхождения формулы в документ: страница и число вхождений на ней (tf)."""
    __tablename__ = 'formula_occurrences'

    formula_id = Column(String(32), ForeignKey('formulas.id'), primary_key=True)
    pdf_sha256 = Column(String(64), ForeignKey('pdf_files.sha256'), primary_key=True, index=True)
    page = Column(Integer, primary_key=True)
    tf = Column(Integer, nullable=False, default=1)


class HarvestState(Base):
    """Состояние инкрементального сбора (например, дата последнего успешного обхода OAI-репозитория)."""
    __tablename__ = 'harvest_state'

    key = Column(String(255), primary_key=True)  # например 'oai:urfu'
    last_datestamp = Column(String(32))  # дата, с которой запрашивать изменения при следующем обходе
    updated_at = Column(DateTime, server_default=func.now(), onupdate=func.now())


def get_db_session(database_url: str = None):
    """
    Подключение к БД.

    Адрес берётся из аргумента, затем из переменной окружения DATABASE_URL,
    иначе используется PostgreSQL из docker-compose проекта (localhost:5433).
    """
    url = database_url or os.getenv('DATABASE_URL', DEFAULT_DATABASE_URL)
    # SQLAlchemy 2.1 по умолчанию выбирает драйвер psycopg 3, в проекте установлен psycopg2
    if url.startswith('postgresql://'):
        url = 'postgresql+psycopg2://' + url[len('postgresql://'):]
    engine = create_engine(url)
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()
