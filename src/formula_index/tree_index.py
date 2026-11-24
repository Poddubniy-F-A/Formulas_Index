"""Дерево подстановок - индекс формул для нечёткого поиска.

Формат файла совместим с поиском веб-приложения (get_nearest_formulas):
    {"root": [[потомки], [id формул]]}, потомок - {"символ": [[потомки], [id формул]]}.
Формула записывается в дерево как путь из символов её дерева разбора, перечисленных
обходом в ширину: {"=": [{"+": ["a", "b"]}, "c"]} -> "=", "+", "c", "a", "b".

В индексное дерево сохраняется только исходный вид формулы без генерации
вариантов с перестановками операндов коммутативных операций. Учёт перестановок
планируется реализовать на уровне поиска (параметр нечёткости).
"""

import json
import os
import tempfile
from collections import deque
from typing import List, Optional

from src.formula_index.latex_parser import parse_latex

QUERY_ID = 'query'


def symbols(tree) -> List[str]:
    """Символы дерева разбора в порядке обхода в ширину."""
    result, queue = [], deque([tree])
    while queue:
        node = queue.popleft()
        if isinstance(node, str):
            result.append(node)
        else:
            op, args = next(iter(node.items()))
            result.append(op)
            queue.extend(args)
    return result


def query_tree(tree, formula_id: str = QUERY_ID) -> dict:
    """Путь формулы в виде вложенных словарей - формат запроса для get_nearest_formulas."""
    path = [formula_id]
    for symbol in reversed(symbols(tree)):
        path = {symbol: path}
    return path


def latex_to_query(latex: str) -> Optional[dict]:
    """Запрос поиска из LaTeX (замена convert_formula_to_tree веб-приложения)."""
    parsed = parse_latex(latex)
    return query_tree(parsed[0].tree) if parsed else None


class SubstitutionTree:
    def __init__(self, path: str):
        self.path = path
        if os.path.exists(path):
            with open(path, encoding='utf-8') as f:
                self.data = json.load(f)
        else:
            self.data = {'root': [[], []]}

    def add(self, tree, formula_id: str):
        node = self.data['root']
        for symbol in symbols(tree):
            for child in node[0]:
                if symbol in child:
                    node = child[symbol]
                    break
            else:
                new_node = [[], []]
                node[0].append({symbol: new_node})
                node = new_node
        if formula_id not in node[1]:
            node[1].append(formula_id)

    def save(self):
        """Атомарная запись: веб-приложение не должно прочитать недописанный файл."""
        directory = os.path.dirname(os.path.abspath(self.path))
        os.makedirs(directory, exist_ok=True)
        fd, tmp = tempfile.mkstemp(suffix='.json', dir=directory)
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(self.data, f, ensure_ascii=False, separators=(',', ':'))
        os.replace(tmp, self.path)
