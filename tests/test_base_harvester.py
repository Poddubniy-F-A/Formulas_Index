"""Тесты для базового сборщика (BaseHarvester) и пакетного сохранения.

Запуск: python -m unittest tests.test_base_harvester
"""

import unittest
from unittest.mock import MagicMock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.base_harvester import BaseHarvester, DocumentRecord, HarvestStats, normalize_doi
from src.db import Base, Work, WorkSource


class _TestHarvester(BaseHarvester):
    """Тестовый сборщик с фиксированным набором записей."""

    def __init__(self, records, db_session=None, **kwargs):
        self._records = list(records)
        self._call_count = 0
        super().__init__('test', 'https://test.example', db_session, **kwargs)

    def crawl_for_docs(self, year: int):
        self._call_count += 1
        for record in self._records:
            yield record


def _make_sqlite_session():
    engine = create_engine('sqlite:///:memory:')
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


class TestNormalizeDoi(unittest.TestCase):
    def test_plain_doi(self):
        self.assertEqual(normalize_doi('10.1234/abc'), '10.1234/abc')

    def test_doi_with_https_prefix(self):
        self.assertEqual(normalize_doi('https://doi.org/10.1234/abc'), '10.1234/abc')

    def test_doi_with_dx_prefix(self):
        self.assertEqual(normalize_doi('https://dx.doi.org/10.1234/abc'), '10.1234/abc')

    def test_doi_with_doi_prefix(self):
        self.assertEqual(normalize_doi('doi:10.1234/abc'), '10.1234/abc')

    def test_doi_uppercase(self):
        self.assertEqual(normalize_doi('10.1234/ABC'), '10.1234/abc')

    def test_doi_none(self):
        self.assertIsNone(normalize_doi(None))

    def test_doi_empty(self):
        self.assertIsNone(normalize_doi(''))

    def test_doi_invalid(self):
        self.assertIsNone(normalize_doi('not-a-doi'))


class TestDocumentRecord(unittest.TestCase):
    def test_doi_normalized(self):
        rec = DocumentRecord(external_id='1', doi='HTTPS://DOI.ORG/10.1234/ABC')
        self.assertEqual(rec.doi, '10.1234/abc')

    def test_landing_url_from_doi(self):
        rec = DocumentRecord(external_id='1', doi='10.1234/abc')
        self.assertEqual(rec.landing_url, 'https://doi.org/10.1234/abc')

    def test_landing_url_preserved(self):
        rec = DocumentRecord(external_id='1', doi='10.1234/abc', landing_url='https://example.com/page')
        self.assertEqual(rec.landing_url, 'https://example.com/page')

    def test_no_doi_no_landing(self):
        rec = DocumentRecord(external_id='1', doi=None, landing_url=None)
        self.assertIsNone(rec.landing_url)


class TestHarvestStats(unittest.TestCase):
    def test_merge(self):
        a = HarvestStats(found=5, new_works=2, new_links=2, known=1)
        b = HarvestStats(found=3, new_works=1, new_links=1, known=1)
        a.merge(b)
        self.assertEqual(a.found, 8)
        self.assertEqual(a.new_works, 3)
        self.assertEqual(a.new_links, 3)
        self.assertEqual(a.known, 2)


class TestSaveRecord(unittest.TestCase):
    def setUp(self):
        self.db = _make_sqlite_session()

    def tearDown(self):
        self.db.close()

    def _harvester(self):
        return _TestHarvester([], db_session=self.db)

    def test_save_new_work(self):
        h = self._harvester()
        rec = DocumentRecord(external_id='ext-1', doi='10.1234/test1', title='Test Article', year=2024)
        result = h._save_record(rec)
        self.assertEqual(result, 'work')
        self.db.commit()

        work = self.db.query(Work).filter_by(doi='10.1234/test1').first()
        self.assertIsNotNone(work)
        self.assertEqual(work.title, 'Test Article')
        self.assertEqual(work.year, 2024)

        link = self.db.query(WorkSource).filter_by(source='test', external_id='ext-1').first()
        self.assertIsNotNone(link)
        self.assertEqual(link.work_id, work.id)
        self.assertEqual(link.status, WorkSource.STATUS_NEW)

    def test_save_new_link_for_existing_work(self):
        h = self._harvester()
        # Сначала создаём работу
        rec1 = DocumentRecord(external_id='ext-1', doi='10.1234/test1', title='Article', year=2024)
        h._save_record(rec1)
        self.db.commit()

        # Затем создаём копию той же работы от другого источника или с другим external_id
        rec2 = DocumentRecord(external_id='ext-2', doi='10.1234/test1', title='Article', year=2024)
        result = h._save_record(rec2)
        self.assertEqual(result, 'link')

        links = self.db.query(WorkSource).filter_by(work_id=rec1.work_id if hasattr(rec1, 'work_id') else None).all()
        # work_id не хранится в DocumentRecord, проверяем через запрос
        work = self.db.query(Work).filter_by(doi='10.1234/test1').first()
        links = self.db.query(WorkSource).filter_by(work_id=work.id).all()
        self.assertEqual(len(links), 2)

    def test_save_existing_upsert(self):
        h = self._harvester()
        rec1 = DocumentRecord(external_id='ext-1', doi='10.1234/test1', title='Old Title', year=2023)
        h._save_record(rec1)
        self.db.commit()

        # Повторное сохранение с новыми данными — upsert
        rec2 = DocumentRecord(external_id='ext-1', doi='10.1234/test1', title='New Title', pdf_url='https://example.com/file.pdf')
        result = h._save_record(rec2)
        self.assertIsNone(result)  # known

        link = self.db.query(WorkSource).filter_by(source='test', external_id='ext-1').first()
        self.assertEqual(link.title if hasattr(link, 'title') else None, None)  # WorkSource не имеет title
        self.assertEqual(link.pdf_url, 'https://example.com/file.pdf')
        self.assertEqual(link.status, WorkSource.STATUS_NEW)

    def test_save_without_doi(self):
        h = self._harvester()
        rec = DocumentRecord(external_id='ext-no-doi', title='No DOI Article', year=2024)
        result = h._save_record(rec)
        self.assertEqual(result, 'work')

        work = self.db.query(Work).filter_by(doi=None).first()
        self.assertIsNotNone(work)
        self.assertEqual(work.title, 'No DOI Article')


class TestSaveRecords(unittest.TestCase):
    def setUp(self):
        self.db = _make_sqlite_session()

    def tearDown(self):
        self.db.close()

    def test_batch_save(self):
        records = [
            DocumentRecord(external_id=f'ext-{i}', doi=f'10.1234/test{i}', title=f'Article {i}', year=2024)
            for i in range(7)
        ]
        h = _TestHarvester(records, db_session=self.db)
        stats = h.save_records(records, batch_size=3)

        self.assertEqual(stats.found, 7)
        self.assertEqual(stats.new_works, 7)
        self.assertEqual(stats.new_links, 0)
        self.assertEqual(stats.known, 0)

        self.assertEqual(self.db.query(Work).count(), 7)
        self.assertEqual(self.db.query(WorkSource).count(), 7)

    def test_batch_save_mixed(self):
        rec1 = DocumentRecord(external_id='ext-1', doi='10.1234/shared', title='First', year=2024)
        rec2 = DocumentRecord(external_id='ext-2', doi='10.1234/shared', title='Second', year=2024)
        rec3 = DocumentRecord(external_id='ext-1', doi='10.1234/shared', title='Updated', year=2024)

        h = _TestHarvester([rec1, rec2, rec3], db_session=self.db)
        stats = h.save_records([rec1, rec2, rec3], batch_size=2)

        self.assertEqual(stats.found, 3)
        self.assertEqual(stats.new_works, 1)
        self.assertEqual(stats.new_links, 1)
        self.assertEqual(stats.known, 1)

    def test_harvest_full_cycle(self):
        records = [
            DocumentRecord(external_id='ext-1', doi='10.1234/h1', title='Harvest 1', year=2024),
            DocumentRecord(external_id='ext-2', doi='10.1234/h2', title='Harvest 2', year=2024),
        ]
        h = _TestHarvester(records, db_session=self.db)
        stats = h.harvest(year=2024, limit=2, batch_size=2)

        self.assertEqual(stats.found, 2)
        self.assertEqual(stats.new_works, 2)
        self.assertEqual(self.db.query(Work).count(), 2)

    def test_harvest_limit(self):
        records = [
            DocumentRecord(external_id=f'ext-{i}', doi=f'10.1234/l{i}', title=f'Limit {i}', year=2024)
            for i in range(10)
        ]
        h = _TestHarvester(records, db_session=self.db)
        stats = h.harvest(year=2024, limit=3, batch_size=5)

        self.assertEqual(stats.found, 3)
        self.assertEqual(self.db.query(Work).count(), 3)

    def test_skip_records_without_links(self):
        records = [
            DocumentRecord(external_id='ext-1', doi='10.1234/ok', title='OK', year=2024,
                           pdf_url='https://example.com/file.pdf'),
            DocumentRecord(external_id='ext-2', doi=None, title='No links', year=2024),
            DocumentRecord(external_id='ext-3', doi='10.1234/ok2', title='OK2', year=2024,
                           landing_url='https://example.com/page'),
        ]
        h = _TestHarvester(records, db_session=self.db)
        stats = h.harvest(year=2024, batch_size=5)

        # only ext-1 and ext-3 have links
        self.assertEqual(stats.found, 2)
        self.assertEqual(stats.new_works, 2)


if __name__ == '__main__':
    unittest.main()
