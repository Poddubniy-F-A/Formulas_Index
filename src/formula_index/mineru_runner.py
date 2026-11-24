"""Распознавание PDF с помощью MinerU. Запускается интерпретатором окружения MinerU:

    python mineru_runner.py <pdf> <out.json> [basic|standard]

Результат - JSON-список формул: {"page": номер страницы с 1, "latex": ..., "display": bool}.
Модуль не импортирует ничего из проекта: окружение MinerU отдельное.
"""

import json
import os
import shutil
import sys
import tempfile

sys.setrecursionlimit(100000)

FORMULA_TYPES = {'equation': True, 'equation_inline': False}  # тип блока MinerU -> выносная формула


def collect(node, page: int, out: list):
    if isinstance(node, dict):
        kind = node.get('type')
        if kind in FORMULA_TYPES and isinstance(node.get('content'), str) and node['content'].strip():
            out.append({'page': page, 'latex': node['content'], 'display': FORMULA_TYPES[kind]})
        for value in node.values():
            collect(value, page, out)
    elif isinstance(node, list):
        for value in node:
            collect(value, page, out)


def main():
    from mineru import parse

    pdf, out_path = sys.argv[1], sys.argv[2]
    tier = sys.argv[3] if len(sys.argv) > 3 else 'basic'
    # путь с кириллицей ломает часть библиотек MinerU - работаем с копией во временном каталоге
    with tempfile.TemporaryDirectory(prefix='mineru_') as tmp:
        local = os.path.join(tmp, 'document.pdf')
        shutil.copyfile(pdf, local)
        result = parse(local, tier=tier, image_analysis=False).to_dict()
    formulas = []
    for page in result.get('pages', []):
        collect(page.get('blocks', []), page.get('page_idx', 0) + 1, formulas)
    with open(out_path, 'w', encoding='utf-8') as f:
        json.dump({'pages': len(result.get('pages', [])), 'formulas': formulas}, f, ensure_ascii=False)


if __name__ == '__main__':  # обязательно на Windows: MinerU запускает дочерние процессы
    main()
