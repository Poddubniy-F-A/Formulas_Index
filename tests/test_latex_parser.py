"""Проверка разбора LaTeX: python -m unittest discover tests"""

import unittest

from src.formula_index import parse_latex
from src.formula_index.tree_index import symbols, variants


def trees(latex):
    return [p.tree for p in parse_latex(latex)]


class ParserTest(unittest.TestCase):
    def check(self, latex, expected):
        parsed = parse_latex(latex)
        self.assertEqual([p.tree for p in parsed], expected, latex)
        self.assertTrue(all(p.skipped == 0 for p in parsed), latex)

    def test_basic(self):
        self.check(r'a+b=c', [{'=': [{'+': ['a', 'b']}, 'c']}])
        self.check(r'E=m c^{2}', [{'=': ['E', {'*': ['m', {'^': ['c', '2']}]}]}])
        self.check(r'\frac{\partial u}{\partial t}=\Delta u',
                   [{'=': [{'\\frac': [{'*': ['\\partial', 'u']}, {'*': ['\\partial', 't']}]},
                           {'*': ['\\Delta', 'u']}]}])
        self.check(r'\sum_{i=1}^{n} a_{i}', [{'\\sum': [{'=': ['i', '1']}, 'n', 'a_i']}])
        self.check(r'\|x\|_{2} \leq \varepsilon', [{'\\leq': [{'_': [{'‖': ['x']}, '2']}, '\\varepsilon']}])

    def test_mineru_spacing(self):
        """MinerU пишет LaTeX поэлементно, с пробелами."""
        self.assertEqual(trees(r'P _ { n } = \operatorname* { m a x } x'), trees(r'P_n = \max x'))
        self.check(r'x ^ { 1 0 } = 1 0 2 4', [{'=': [{'^': ['x', '10']}, '1024']}])

    def test_numbers(self):
        self.check(r'R ^ { 2 } = 0 . 9 3 8 9', [{'=': [{'^': ['R', '2']}, '0.9389']}])
        self.check(r'r ^ { 2 } = 0 { , } 8 9', [{'=': [{'^': ['r', '2']}, '0.89']}])
        self.check(r'n = 0,26', [{'=': ['n', '0.26']}])
        self.check(r'a = 10\,000 + 2.5', [{'=': ['a', {'+': ['10000', '2.5']}]}])
        self.check(r'i = 1 , 2 , \dots , n', [{'=': ['i', '1']}])  # перечисление: запятая - разделитель
        self.check(r'x \in [ 0 , 1 ]', [{'\\in': ['x', {',': ['0', '1']}]}])  # интервал

    def test_vectors(self):
        self.check(r'\stackrel { \rightharpoonup } { q _ { e } } = \frac { ( C _ { 0 } - C _ { M } ) \cdot V } { m }',
                   [{'=': [{'\\vec': ['q_e']}, {'\\frac': [{'*': [{'-': ['C_0', 'C_M']}, 'V']}, 'm']}]}])
        self.assertEqual(trees(r'\stackrel{\to}{v}'), trees(r'\vec{v}'))

    def test_lone_symbols(self):
        self.check(r'f^{*}(z) = g(\cdot)', [{'=': [{'*': [{'^': ['f', '*']}, 'z']}, {'g': ['\\cdot']}]}])


class TreeIndexTest(unittest.TestCase):
    def test_bfs_path(self):
        self.assertEqual(symbols({'=': [{'+': ['a', 'b']}, 'c']}), ['=', '+', 'c', 'a', 'b'])

    def test_variants_are_limited(self):
        tree = trees('+'.join(f'a_{{{i}}} b_{{{i}}}' for i in range(12)) + '= 0')[0]
        self.assertLessEqual(len(variants(tree)), 100)
        self.assertIn(tree, variants(tree))


if __name__ == '__main__':
    unittest.main()
