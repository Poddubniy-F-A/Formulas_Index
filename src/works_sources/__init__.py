from src.works_sources.crossref import CrossrefHarvester
from src.works_sources.doaj import DoajHarvester
from src.works_sources.oai_pmh import OaiPmhHarvester

# идентификатор источника -> класс обходчика
# Источники документов, опубликованных в России:
# - Crossref: статьи российских издателей (основной реестр DOI);
# - DOAJ: российские журналы открытого доступа - статьи без DOI и прямые ссылки на PDF;
# - OAI-PMH: репозитории российских университетов (диссертации, препринты и т. п.).
HARVESTERS = {
    'crossref': CrossrefHarvester,
    'doaj': DoajHarvester,
    'oai': OaiPmhHarvester,
}
