"""Разбор формул LaTeX в дерево для индекса формул.

LaTeX сначала разбирается библиотекой pylatexenc (команды, группы {...}, аргументы
команд), затем по полученным лексемам строится дерево выражения с учётом приоритетов
операций. Формат дерева совместим с деревом подстановок индекса: узел - словарь
{оператор: [операнды]}, лист - строка ('x', '2', '\\alpha', 'a_i').

Разбор никогда не завершается ошибкой: непонятные лексемы пропускаются и считаются
(ParsedFormula.skipped) - это мера качества разбора конкретной формулы.
"""

import hashlib
import json
from dataclasses import dataclass, field
from typing import List, Optional, Union

from pylatexenc.latexwalker import (LatexCharsNode, LatexEnvironmentNode, LatexGroupNode, LatexMacroNode,
                                    LatexMathNode, LatexSpecialsNode, LatexWalker)

Tree = Union[str, dict]

# ---------- словари команд ----------

RELATIONS = {
    '=', '<', '>', ':',
    r'\leq', r'\le', r'\leqslant', r'\geq', r'\ge', r'\geqslant', r'\neq', r'\ne', r'\approx', r'\equiv',
    r'\sim', r'\simeq', r'\cong', r'\propto', r'\ll', r'\gg', r'\in', r'\notin', r'\ni', r'\subset',
    r'\subseteq', r'\supset', r'\supseteq', r'\to', r'\rightarrow', r'\Rightarrow', r'\leftarrow',
    r'\Leftarrow', r'\leftrightarrow', r'\Leftrightarrow', r'\iff', r'\implies', r'\mapsto', r'\parallel',
    r'\perp', r'\mid', r'\prec', r'\succ', r'\preceq', r'\succeq', r'\models', r'\vdash',
}
# нормализация синонимов отношений
RELATION_ALIASES = {r'\le': r'\leq', r'\leqslant': r'\leq', r'\ge': r'\geq', r'\geqslant': r'\geq',
                    r'\ne': r'\neq', r'\to': r'\rightarrow', r'\implies': r'\Rightarrow', r'\iff': r'\Leftrightarrow'}
ADDITIVE = {'+', '-', r'\pm', r'\mp', r'\cup', r'\setminus', r'\oplus', r'\vee', r'\lor', r'\sqcup'}
MULTIPLICATIVE = {'*', '/', r'\cdot', r'\times', r'\div', r'\circ', r'\otimes', r'\cap', r'\wedge', r'\land',
                  r'\sqcap', r'\bullet', r'\ast', r'\star'}
MUL_ALIASES = {r'\cdot': '*', r'\times': '*', r'\ast': '*', r'\bullet': '*', r'\land': r'\wedge',
               r'\lor': r'\vee'}
# операторы с пределами: тело - всё следующее аддитивное выражение
BIG_OPERATORS = {r'\sum', r'\prod', r'\coprod', r'\int', r'\intop', r'\iint', r'\iiint', r'\oint', r'\bigcup', r'\bigcap',
                 r'\bigoplus', r'\bigotimes', r'\bigvee', r'\bigwedge'}
# операторы с индексом снизу, применяемые к следующему множителю
LIMIT_OPERATORS = {r'\lim', r'\limsup', r'\liminf', r'\max', r'\min', r'\sup', r'\inf', r'\det', r'\gcd',
                   r'\Pr', r'\arg', r'\esssup', r'\essinf'}
FUNCTIONS = {r'\sin', r'\cos', r'\tan', r'\cot', r'\sec', r'\csc', r'\arcsin', r'\arccos', r'\arctan',
             r'\sinh', r'\cosh', r'\tanh', r'\coth', r'\ln', r'\log', r'\lg', r'\exp', r'\deg', r'\dim',
             r'\ker', r'\hom', r'\Re', r'\Im', r'\sgn', r'\tr', r'\rank', r'\div', r'\grad', r'\rot', r'\mod'}
FUNCTIONS -= MULTIPLICATIVE
FRACTIONS = {r'\frac', r'\dfrac', r'\tfrac', r'\cfrac'}
ACCENTS = {r'\bar', r'\hat', r'\tilde', r'\vec', r'\dot', r'\ddot', r'\widetilde', r'\widehat', r'\overline',
           r'\underline', r'\overrightarrow', r'\overleftarrow', r'\check', r'\breve', r'\acute', r'\grave'}
# оформление, не меняющее смысла: аргумент подставляется как есть
STYLE_WRAPPERS = {'mathbb', 'mathbf', 'mathrm', 'mathit', 'mathcal', 'mathfrak', 'mathscr', 'mathsf', 'mathtt',
                  'boldsymbol', 'bm', 'pmb', 'rm', 'bf', 'it', 'cal', 'displaystyle', 'textstyle',
                  'scriptstyle', 'mathop', 'mathord', 'mathrel', 'mathbin', 'operatorname*'}
TEXT_WRAPPERS = {'text', 'textrm', 'textit', 'textbf', 'textsc', 'textsf', 'mbox', 'mathrm', 'operatorname'}
IGNORED_MACROS = {',', ';', ':', '!', ' ', 'quad', 'qquad', 'thinspace', 'enspace', 'medspace', 'thickspace',
                  'negthinspace', 'hspace', 'vspace', 'left', 'right', 'big', 'Big', 'bigg', 'Bigg', 'bigl',
                  'bigr', 'Bigl', 'Bigr', 'biggl', 'biggr', 'middle', 'limits', 'nolimits', 'nonumber',
                  'notag', 'label', 'tag', 'qed', 'hfill', 'phantom', 'hphantom', 'vphantom', 'allowbreak',
                  'displaylimits', 'tiny', 'scriptsize', 'footnotesize', 'small', 'normalsize', 'large',
                  'Large', 'huge', 'eqno', 'leqno', 'strut', 'nobreak'}
# служебные команды с аргументом в {...}: аргумент тоже отбрасывается (	ag{1}, \label{eq})
IGNORED_WITH_ARGUMENT = {'tag', 'label', 'hspace', 'vspace', 'phantom', 'hphantom', 'vphantom', 'eqno', 'leqno'}
ARROWS = {r'\rightarrow', r'\to', r'\rightharpoonup', r'\leftarrow', r'\leftrightarrow', r'\longrightarrow', '→'}
DOTS = {r'\dots', r'\ldots', r'\cdots', r'\vdots', r'\ddots', r'\dotsc', r'\dotsb'}
SYMBOL_MACROS = {'|': '‖', '{': '{', '}': '}', 'lbrace': '{', 'rbrace': '}', 'langle': '⟨', 'rangle': '⟩',
                 'lvert': '|', 'rvert': '|', 'vert': '|', 'lVert': '‖', 'rVert': '‖', 'Vert': '‖',
                 'lfloor': '⌊', 'rfloor': '⌋', 'lceil': '⌈', 'rceil': '⌉', '\\': '\\\\'}
OPENERS = {'(': ')', '[': ']', '{': '}', '⟨': '⟩', '⌊': '⌋', '⌈': '⌉'}
SYMMETRIC = {'|', '‖'}  # открывающий и закрывающий символ совпадают
SEPARATORS = {',', ';', '&', '\\\\'}


@dataclass
class Tok:
    kind: str  # num | letter | sym | macro | group
    value: str = ''
    args: List[List['Tok']] = field(default_factory=list)  # аргументы команды (уже лексемы)
    items: List['Tok'] = field(default_factory=list)  # содержимое группы


@dataclass
class ParsedFormula:
    tree: Tree
    skipped: int = 0  # сколько лексем пропущено при разборе

    @property
    def canonical(self) -> str:
        return canonical(self.tree)

    @property
    def id(self) -> str:
        return hashlib.md5(self.canonical.encode()).hexdigest()


# ---------- лексемы ----------

def _chars_to_tokens(chars: str) -> List[Tok]:
    tokens, i = [], 0
    while i < len(chars):
        c = chars[i]
        if c.isspace():
            i += 1
        elif c.isdigit():
            j = i
            while j < len(chars) and chars[j].isdigit():
                j += 1
            tokens.append(Tok('num', chars[i:j]))
            i = j
        elif c.isalpha():
            tokens.append(Tok('letter', c))
            i += 1
        else:
            tokens.append(Tok('sym', c))
            i += 1
    return tokens


def _merge_numbers(tokens: List[Tok]) -> List[Tok]:
    """
    Склеивает числа, которые MinerU записывает поцифренно: '1 0' -> 10, '1 . 9' -> 1.9,
    '10\\,000' -> 10000. Десятичная запятая (русские тексты: '0 , 26') -> 0.26, кроме
    перечислений '1, 2, \\dots' и интервалов '[0, 1]'. Точка вне числа - пунктуация, отбрасывается.
    """
    def kind(k):
        return tokens[k].kind if 0 <= k < len(tokens) else None

    def value(k):
        return tokens[k].value if 0 <= k < len(tokens) else None

    result: List[Tok] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok.kind == 'num' and result and result[-1].kind == 'num':
            result[-1] = Tok('num', result[-1].value + tok.value)
        elif (tok.kind == 'sym' and tok.value in '.,' and result and result[-1].kind == 'num'
              and '.' not in result[-1].value and kind(i + 1) == 'num'):
            start = i - 1  # первая цифра числа перед разделителем
            while kind(start - 1) == 'num':
                start -= 1
            end = i + 1  # последняя цифра числа после разделителя
            while kind(end + 1) == 'num':
                end += 1
            is_list = value(start - 1) == ',' or value(end + 1) == ',' or value(end + 1) in DOTS
            is_interval = value(start - 1) in OPENERS and value(end + 1) in OPENERS.values()
            if tok.value == ',' and (is_list or is_interval):
                result.append(tok)
            else:
                result[-1] = Tok('num', result[-1].value + '.')
        elif tok.kind == 'sym' and tok.value == '.':
            pass
        else:
            result.append(tok)
        i += 1
    return result


def _is_relation(tok: Tok) -> bool:
    return tok.kind in ('sym', 'macro') and not tok.args and tok.value in RELATIONS


def _arg_tokens(arg) -> List[Tok]:
    if arg is None:
        return []
    if isinstance(arg, LatexGroupNode):
        return _nodes_to_tokens(arg.nodelist)
    return _nodes_to_tokens([arg])


def _nodes_to_tokens(nodes) -> List[Tok]:
    tokens: List[Tok] = []
    nodes = list(nodes or [])
    i = 0
    while i < len(nodes):
        node = nodes[i]
        if isinstance(node, LatexCharsNode):
            tokens += _chars_to_tokens(node.chars)
        elif isinstance(node, LatexGroupNode):
            items = _nodes_to_tokens(node.nodelist)
            # {=} в выровненных формулах; {,} - десятичная запятая: 0{,}89
            if len(items) == 1 and (_is_relation(items[0]) or (items[0].kind == 'sym' and items[0].value == ',')):
                tokens.append(items[0])
            else:
                tokens.append(Tok('group', items=items))
        elif isinstance(node, (LatexMathNode, LatexEnvironmentNode)):
            tokens += _nodes_to_tokens(node.nodelist)
        elif isinstance(node, LatexSpecialsNode):
            if node.specials_chars in ('&', '\\\\'):
                tokens.append(Tok('sym', node.specials_chars))
        elif isinstance(node, LatexMacroNode):
            name = node.macroname
            args = [a for a in (node.nodeargd.argnlist if node.nodeargd else []) if a is not None]
            if name in SYMBOL_MACROS:
                tokens.append(Tok('sym', SYMBOL_MACROS[name]))
            elif name in IGNORED_MACROS:
                if (name in IGNORED_WITH_ARGUMENT and not args and i + 1 < len(nodes)
                        and isinstance(nodes[i + 1], LatexGroupNode)):
                    i += 1
            elif name in ('stackrel', 'overset', 'underset'):
                # \stackrel{\rightharpoonup}{q} - вектор; иначе {'\overset': [надпись, основа]}
                parts = [_arg_tokens(a) for a in args]
                while len(parts) < 2:
                    j = i + 1  # между группами '{ a } { b }' стоит узел-пробел
                    while j < len(nodes) and isinstance(nodes[j], LatexCharsNode) and not nodes[j].chars.strip():
                        j += 1
                    if j == len(nodes) or not isinstance(nodes[j], LatexGroupNode):
                        break
                    parts.append(_nodes_to_tokens(nodes[j].nodelist))
                    i = j
                if len(parts) == 2:
                    top, base = parts
                    if len(top) == 1 and top[0].value in ARROWS:
                        tokens.append(Tok('macro', r'\vec', args=[base]))
                    else:
                        tokens.append(Tok('macro', '\\' + ('underset' if name == 'underset' else 'overset'),
                                          args=[top, base]))
            elif name == 'operatorname':
                # \operatorname*{m a x} -> \max: аргумент - следующая группа (после необязательной '*')
                j = i + 1
                while j < len(nodes) and isinstance(nodes[j], LatexCharsNode) and nodes[j].chars.strip() in ('', '*'):
                    j += 1
                if j < len(nodes) and isinstance(nodes[j], LatexGroupNode):
                    word = ''.join(t.value for t in _nodes_to_tokens(nodes[j].nodelist) if t.kind == 'letter')
                    tokens.append(Tok('macro', '\\' + word))
                    i = j
            elif name in TEXT_WRAPPERS or name in STYLE_WRAPPERS:
                content = _arg_tokens(args[0]) if args else []
                if not args and i + 1 < len(nodes) and isinstance(nodes[i + 1], LatexGroupNode):
                    content = _nodes_to_tokens(nodes[i + 1].nodelist)
                    i += 1
                if name in TEXT_WRAPPERS and content and all(t.kind == 'letter' for t in content) and len(content) > 1:
                    tokens.append(Tok('letter', ''.join(t.value for t in content)))  # слово \text{max} - один лист
                else:
                    tokens += content
            else:
                tokens.append(Tok('macro', '\\' + name, args=[_arg_tokens(a) for a in args]))
        i += 1
    return _merge_numbers(tokens)


def tokenize(latex: str) -> List[Tok]:
    try:
        nodes, _, _ = LatexWalker(latex, tolerant_parsing=True).get_latex_nodes()
    except Exception:
        return _merge_numbers(_chars_to_tokens(latex))
    return _nodes_to_tokens(nodes)


# ---------- разбор выражения ----------

class _Parser:
    def __init__(self, tokens: List[Tok]):
        self.tokens = tokens
        self.pos = 0
        self.skipped = 0
        self.closers: List[str] = []  # ожидаемые закрывающие символы (для |...| и скобок)

    # служебное
    def peek(self, offset=0) -> Optional[Tok]:
        i = self.pos + offset
        return self.tokens[i] if i < len(self.tokens) else None

    def take(self) -> Tok:
        tok = self.tokens[self.pos]
        self.pos += 1
        return tok

    @staticmethod
    def op_of(tok: Optional[Tok]) -> Optional[str]:
        if tok is None:
            return None
        if tok.kind == 'sym' or (tok.kind == 'macro' and not tok.args):
            return tok.value
        return None

    def at_stop(self) -> bool:
        tok = self.peek()
        if tok is None:
            return True
        op = self.op_of(tok)
        return tok.kind == 'sym' and (op in SEPARATORS or op in self.closers or op in OPENERS.values())

    def sub(self, tokens: List[Tok]) -> Tree:
        parser = _Parser(tokens)
        tree = parser.parse_sequence()
        self.skipped += parser.skipped
        return tree

    # уровни приоритета
    def parse_sequence(self) -> Tree:
        """Выражение целиком; запятые внутри скобок дают кортеж."""
        items = [self.parse_relation()]
        while self.peek() is not None and self.op_of(self.peek()) == ',' and ',' not in self.closers:
            self.take()
            items.append(self.parse_relation())
        while self.peek() is not None and not self.closers:  # хвост, который не удалось разобрать
            self.take()
            self.skipped += 1
        items = [i for i in items if i != '']
        if not items:
            return ''
        return items[0] if len(items) == 1 else {',': items}

    def parse_relation(self) -> Tree:
        left = self.parse_additive()
        op = self.op_of(self.peek())
        if op in RELATIONS:
            self.take()
            if op == ':' and self.op_of(self.peek()) == '=':  # :=
                self.take()
                op = ':='
            right = self.parse_relation()
            return {RELATION_ALIASES.get(op, op): [left, right]}
        return left

    def parse_additive(self) -> Tree:
        left = self.parse_multiplicative()
        op = self.op_of(self.peek())
        if op in ADDITIVE and not self.at_stop():
            self.take()
            right = self.parse_additive()
            return {MUL_ALIASES.get(op, op): [left, right]}
        return left

    def starts_operand(self) -> bool:
        tok = self.peek()
        if tok is None or self.at_stop():
            return False
        op = self.op_of(tok)
        if tok.kind == 'sym':
            return op in OPENERS or (op in SYMMETRIC and op not in self.closers)
        if tok.kind == 'macro' and not tok.args:
            return op not in RELATIONS and op not in ADDITIVE and op not in MULTIPLICATIVE
        return True

    def parse_multiplicative(self) -> Tree:
        left = self.parse_unary()
        op = self.op_of(self.peek())
        if op in MULTIPLICATIVE and not self.at_stop():
            self.take()
            right = self.parse_multiplicative()
            return {MUL_ALIASES.get(op, op): [left, right]}
        if self.starts_operand():  # неявное умножение: 2ab, x f(x)
            right = self.parse_multiplicative()
            if right == '':
                return left
            return {'*': [left, right]} if left != '' else right
        return left

    def parse_unary(self) -> Tree:
        op = self.op_of(self.peek())
        if op in ('-', '+', r'\pm', r'\mp', r'\neg', r'\lnot'):
            self.take()
            operand = self.parse_unary()
            return operand if op == '+' else {op: [operand]}
        return self.parse_postfix()

    def parse_script(self) -> Tree:
        """Аргумент индекса или степени: группа или одна лексема."""
        tok = self.peek()
        if tok is None:
            return ''
        if tok.kind == 'group':
            return self.sub(self.take().items)
        return self.parse_primary()

    def parse_postfix(self, base: Tree = None) -> Tree:
        if base is None:
            base = self.parse_primary()
        while True:
            op = self.op_of(self.peek())
            if op == '_':
                self.take()
                index = self.parse_script()
                if isinstance(base, str) and isinstance(index, str) and base and index:
                    base = f'{base}_{index}'
                else:
                    base = {'_': [base, index]}
            elif op == '^':
                self.take()
                base = {'^': [base, self.parse_script()]}
            elif op in ("'", '!'):
                self.take()
                base = {op: [base]}
            else:
                return base

    def parse_scripts(self):
        """Пределы оператора: _{...} и ^{...} в любом порядке."""
        lower = upper = None
        while self.op_of(self.peek()) in ('_', '^'):
            op = self.take().value
            value = self.parse_script()
            if op == '_':
                lower = value
            else:
                upper = value
        return lower, upper

    def parse_bracketed(self, opener: str) -> Tree:
        closer = OPENERS.get(opener, opener)
        self.closers.append(closer)
        inner = self.parse_bracket_content(closer)
        self.closers.pop()
        if self.op_of(self.peek()) == closer:
            self.take()
        if opener in SYMMETRIC:
            return {opener: [inner]}  # модуль |x|, норма ‖x‖
        if opener == '{':
            return {'{}': [inner]}  # множество
        return inner

    def parse_bracket_content(self, closer: str) -> Tree:
        items = [self.parse_relation()]
        while self.op_of(self.peek()) in (',', ';'):
            self.take()
            items.append(self.parse_relation())
        # непонятные лексемы до закрывающей скобки пропускаются
        while self.peek() is not None and self.op_of(self.peek()) != closer and closer in self.closers:
            depth_closer = self.op_of(self.peek())
            if depth_closer in self.closers[:-1]:
                break
            self.take()
            self.skipped += 1
        items = [i for i in items if i != '']
        if not items:
            return ''
        return items[0] if len(items) == 1 else {',': items}

    def parse_function_argument(self) -> Tree:
        if self.op_of(self.peek()) in OPENERS and self.peek().kind == 'sym':
            return self.parse_postfix(self.parse_bracketed(self.take().value))
        return self.parse_unary()

    def parse_primary(self) -> Tree:
        tok = self.peek()
        if tok is None:
            return ''
        if tok.kind == 'num':
            return self.take().value
        if tok.kind == 'letter':
            name = self.take().value
            # f(x) - применение функции (как в исходной грамматике индекса)
            if self.op_of(self.peek()) == '(' and len(name) == 1:
                self.take()
                self.closers.append(')')
                args = self.parse_bracket_content(')')
                self.closers.pop()
                if self.op_of(self.peek()) == ')':
                    self.take()
                return {name: args[','] if isinstance(args, dict) and ',' in args else [args]}
            return name
        if tok.kind == 'group':
            return self.sub(self.take().items)
        if self.is_lone_symbol(tok):
            return self.take().value
        if tok.kind == 'sym':
            op = tok.value
            if op in OPENERS or op in SYMMETRIC:
                self.take()
                return self.parse_bracketed(op)
            self.take()  # знак, которого здесь не ожидалось
            self.skipped += 1
            return ''
        # команда
        self.take()
        name = tok.value
        if name in FRACTIONS:
            args = tok.args or [self.parse_script_tokens(), self.parse_script_tokens()]
            parts = [self.sub(a) if isinstance(a, list) else a for a in args[:2]]
            return {r'\frac': parts}
        if name == r'\sqrt':
            args = [self.sub(a) for a in tok.args if a] or [self.parse_script()]
            return {r'\sqrt': args}
        if name in ACCENTS:
            arg = self.sub(tok.args[0]) if tok.args else self.parse_script()
            return {name: [arg]}
        if name in BIG_OPERATORS:
            name = r'\int' if name == r'\intop' else name
            lower, upper = self.parse_scripts()
            body = self.parse_additive()
            return {name: [x for x in (lower, upper, body) if x is not None]}
        if name in LIMIT_OPERATORS:
            lower, upper = self.parse_scripts()
            body = self.parse_function_argument()
            return {name: [x for x in (lower, upper, body) if x is not None]}
        if name in FUNCTIONS:
            lower, power = self.parse_scripts()
            arg = self.parse_function_argument()
            node = {name: [x for x in (lower, arg) if x is not None]}
            return {'^': [node, power]} if power is not None else node
        if name in DOTS:
            return r'\dots'
        if tok.args:  # прочие команды с аргументами
            return {name: [self.sub(a) for a in tok.args]}
        if name in RELATIONS or name in ADDITIVE or name in MULTIPLICATIVE:
            self.skipped += 1
            return ''
        return name  # греческие буквы и прочие символы: \alpha, \infty, \partial

    def is_lone_symbol(self, tok: Tok) -> bool:
        r"""Знак без операндов - обозначение, а не операция: f^{*}, f^{+}, g(\cdot)."""
        if tok.kind not in ('sym', 'macro') or tok.args:
            return False
        if tok.value not in ('*', '+', '-', "'", r'\cdot', r'\ast', r'\star', r'\circ', r'\bullet', r'\prime'):
            return False
        nxt = self.peek(1)
        return nxt is None or (nxt.kind == 'sym' and (nxt.value in SEPARATORS or nxt.value in OPENERS.values()
                                                      or nxt.value in self.closers))

    def parse_script_tokens(self) -> List[Tok]:
        tok = self.peek()
        if tok is None:
            return []
        self.take()
        return tok.items if tok.kind == 'group' else [tok]


# ---------- внешний интерфейс ----------

def _split_top_level(tokens: List[Tok]) -> List[List[Tok]]:
    """Делит формулу на части по разделителям верхнего уровня (, ; & \\\\)."""
    parts, current, depth = [], [], 0
    for tok in tokens:
        if tok.kind == 'sym' and tok.value in OPENERS:
            depth += 1
        elif tok.kind == 'sym' and tok.value in OPENERS.values():
            depth = max(0, depth - 1)
        if depth == 0 and tok.kind == 'sym' and tok.value in SEPARATORS:
            if current:
                parts.append(current)
            current = []
        else:
            current.append(tok)
    if current:
        parts.append(current)
    return parts


def parse_latex(latex: str) -> List[ParsedFormula]:
    """
    Разбирает формулу LaTeX. Перечисления верхнего уровня (a = 1, b = 2) дают
    отдельные формулы. Возвращаются только формулы, содержащие операцию: одиночные
    символы (x, \\alpha) в индекс не попадают - как и в исходной реализации.
    """
    result = []
    for part in _split_top_level(tokenize(latex)):
        parser = _Parser(part)
        tree = parser.parse_sequence()
        if isinstance(tree, dict):
            result.append(ParsedFormula(tree, parser.skipped))
    return result


def canonical(tree: Tree) -> str:
    """Каноническая запись дерева (для идентификатора формулы)."""
    return json.dumps(tree, ensure_ascii=False, separators=(',', ':'))
