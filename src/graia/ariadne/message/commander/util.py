from __future__ import annotations

import enum
import functools
import inspect
import re
from copy import copy
from typing import (
    Any,
    Dict,
    FrozenSet,
    Generic,
    Iterable,
    List,
    Optional,
    Set,
    TypeVar,
    Union,
)

from graia.broadcast.entities.decorator import Decorator
from graia.broadcast.entities.dispatcher import BaseDispatcher
from graia.broadcast.interfaces.dispatcher import DispatcherInterface
from typing_extensions import Self

from ...typing import MaybeFlag, Sentinel, T
from ..parser.util import split as split  # noqa: F401

L_PAREN = ("{", "[")
R_PAREN = ("}", "]")
ESCAPE = {
    "\\": "\x00",
    "[": "\x01",
    "]": "\x02",
    "{": "\x03",
    "}": "\x04",
    "|": "\x05",
}
R_ESCAPE = {v: k for k, v in ESCAPE.items()}


def escape(string: str) -> str:
    """转义字符串

    Args:
        string (str): 要转义的字符串

    Returns:
        str: 转义后的字符串
    """
    for k, v in ESCAPE.items():
        string = string.replace("\\" + k, v)
    string = string.replace("\\", "")
    return string


def unescape(string: str) -> str:
    """逆转义字符串, 自动去除空白符

    Args:
        string (str): 要逆转义的字符串

    Returns:
        str: 逆转义后的字符串
    """
    for k, v in R_ESCAPE.items():
        string = string.replace(k, v)
    return string.strip()


class Text:
    __slots__ = "choice"
    choice: FrozenSet[str]

    def __init__(self, choice: Union[Iterable[str], str]) -> None:
        self.choice = frozenset((choice,)) if isinstance(choice, str) else frozenset(choice)

    def __repr__(self) -> str:
        return f"Text({self.choice!r})"


class Param:
    __slots__ = ("names",)
    names: FrozenSet[str]

    def __init__(self, names: Iterable[str]) -> None:
        self.names = frozenset(names)

    def __repr__(self) -> str:
        return f"Param({self.names!r})"


class AnnotatedParam:
    __slots__ = ("name", "annotation", "default", "wildcard")
    name: str
    annotation: Optional[str]
    default: Optional[str]
    wildcard: bool

    def __init__(
        self,
        name: str,
        wildcard: bool = False,
        annotation: Optional[str] = None,
        default: Optional[str] = None,
    ) -> None:
        self.name = name
        self.annotation = annotation
        self.default = default
        self.wildcard = wildcard

    def __repr__(self) -> str:
        return (
            f"AnnotatedParam({'...' if self.wildcard else ''}{self.name}: {self.annotation} = {self.default})"
        )

    def to_param(self) -> Param:
        return Param((self.name,))


U_Token = Union[Text, Param, AnnotatedParam]

ann_assign = re.compile(r"(?P<name>[^:=]+)(?P<annotation>:[^=]+)?(?P<default>=.+)?")


def parse_param(param_str: str) -> Union[Param, AnnotatedParam]:
    wildcard: bool = param_str.startswith("...")
    if wildcard:
        param_str = param_str[3:]
    match = ann_assign.match(param_str)
    assert match, f"Invalid param: {param_str}"
    names, *extra = match.groups()
    names = names.split("|")
    if not extra:
        return Param(map(str.strip, names))
    assert len(names) == 1, f"Invalid param: {param_str}"
    return AnnotatedParam(names[0], wildcard, *map(lambda s: unescape(s).lstrip(":=").strip(), extra))


def _pop(char_stk: List[str]) -> str:
    piece = "".join(char_stk)
    char_stk.clear()
    return piece


def tokenize(string: str) -> List[U_Token]:
    """将字符串转义化, 并处理为 Text,  Param 两种 token

    Args:
        string (str): 要处理的字符串

    Returns:
        List[Tuple[CommandToken, List[int, str]]]: 处理后的 Token
    """

    string = escape(string)

    paren: str = ""
    char_stk: List[str] = []
    token: List[U_Token] = []
    pop = functools.partial(_pop, char_stk)

    for index, char in enumerate(string):
        if char in L_PAREN + R_PAREN:
            if char in L_PAREN:
                assert not paren, (
                    f"""Duplicated parenthesis character "{char}" @ {index} !"""
                    """Are you sure you've escaped with "\\"?"""
                )
                paren = char
            elif char in R_PAREN:
                piece = pop()
                assert paren, f"No matching parenthesis: {paren} @ {index}"
                if paren == "[":  # CHOICE
                    token.append(Text(unescape(x) for x in piece.split("|")))
                elif paren == "{":  # PARAM
                    token.append(parse_param(piece))
                paren = ""
        elif char == " " and not paren:
            if char_stk:
                token.append(Text(pop()))
        else:
            char_stk.append(char)

    assert not paren, f"Unclosed parenthesis: {paren}"

    if char_stk:
        token.append(Text(pop()))

    return token


class MatchEntry:
    def __init__(self, tokens: List[U_Token]) -> None:
        self.nodes: List[MaybeFlag[FrozenSet[str]]] = [
            token.choice if isinstance(token, Text) else Sentinel for token in tokens
        ]
        self.tokens: List[Union[Text, Param]] = [
            token.to_param() if isinstance(token, AnnotatedParam) else token for token in tokens
        ]


T_MatchEntry = TypeVar("T_MatchEntry", bound=MatchEntry)


class MatchNode(Generic[T_MatchEntry]):
    __slots__ = ("next", "entries")
    next: Dict[MaybeFlag[str], MatchNode[T_MatchEntry]]
    entries: List[T_MatchEntry]

    def __init__(self) -> None:
        self.next = {}
        self.entries = []

    def __copy__(self) -> Self:
        new_obj = self.__class__()
        new_obj.next = self.next.copy()
        new_obj.entries = self.entries.copy()
        return new_obj

    def push(self, entry: T_MatchEntry, index: int = 0) -> None:
        if index >= len(entry.nodes):
            self.entries.append(entry)
            return
        current: MaybeFlag[FrozenSet[str]] = entry.nodes[index]
        if current is Sentinel:
            self.next.setdefault(current, MatchNode()).push(entry, index + 1)
        else:
            target_nodes: List[MatchNode] = []
            conflicts: Dict[MatchNode, Set[str]] = {}
            for piece, node in {piece: self.next[piece] for piece in current if piece in self.next}.items():
                conflicts.setdefault(node, set()).add(piece)
            for old_node, conflict_fields in conflicts.items():
                new_node = copy(old_node)
                target_nodes.append(new_node)
                for field in conflict_fields:
                    self.next[field] = new_node
                new_node.push(entry, index + 1)
                current -= conflict_fields
            if current:
                new_node = MatchNode()
                new_node.push(entry, index + 1)
                for field in current:
                    self.next[field] = new_node

    def _inspect(self, fwd=""):
        if not self.next:
            print(fwd)
        for k, node in self.next.items():
            node._inspect(f"{fwd}.{'?' if k is Sentinel else k}")


class _RawEnum(enum.Enum):
    Raw = object()


raw = _RawEnum.Raw  # wildcard annotation object


def convert_empty(obj: T) -> MaybeFlag[T]:
    if obj is inspect.Parameter.empty:
        return Sentinel
    if isinstance(obj, Decorator):
        return Sentinel
    return obj


class ConstantDispatcher(BaseDispatcher):
    """分发常量给指定名称的参数"""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.data = data

    async def catch(self, interface: DispatcherInterface):
        return self.data.get(interface.name)
