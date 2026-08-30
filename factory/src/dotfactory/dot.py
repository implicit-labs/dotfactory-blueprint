"""Strict, dependency-free DOT subset for workflow authoring."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


class DotSyntaxError(ValueError):
    def __init__(self, source: str, line: int, column: int, message: str) -> None:
        super().__init__(f"{source}:{line}:{column}: {message}")
        self.source = source
        self.line = line
        self.column = column
        self.message = message


@dataclass(frozen=True)
class Token:
    kind: str
    value: str
    line: int
    column: int


@dataclass(frozen=True)
class LocatedAttributes:
    values: dict[str, str]
    line: int
    column: int


@dataclass(frozen=True)
class DotEdge:
    source: str
    target: str
    attributes: LocatedAttributes
    line: int
    column: int


@dataclass(frozen=True)
class DotGraph:
    graph_id: str
    attributes: dict[str, str]
    node_defaults: dict[str, str]
    edge_defaults: dict[str, str]
    nodes: dict[str, LocatedAttributes]
    edges: tuple[DotEdge, ...]
    source_name: str
    source_text: str


PUNCTUATION = {
    "{": "LBRACE",
    "}": "RBRACE",
    "[": "LBRACKET",
    "]": "RBRACKET",
    "=": "EQUAL",
    ",": "COMMA",
    ";": "SEMI",
}


def tokenize(text: str, *, source: str = "<dot>") -> list[Token]:
    tokens: list[Token] = []
    index = 0
    line = 1
    column = 1

    def advance(value: str) -> None:
        nonlocal line, column
        newlines = value.count("\n")
        if newlines:
            line += newlines
            column = len(value.rsplit("\n", 1)[-1]) + 1
        else:
            column += len(value)

    while index < len(text):
        char = text[index]
        if char.isspace():
            advance(char)
            index += 1
            continue
        if text.startswith("//", index) or char == "#":
            end = text.find("\n", index)
            if end == -1:
                end = len(text)
            value = text[index:end]
            advance(value)
            index = end
            continue
        if text.startswith("/*", index):
            end = text.find("*/", index + 2)
            if end == -1:
                raise DotSyntaxError(source, line, column, "unterminated block comment")
            value = text[index:end + 2]
            advance(value)
            index = end + 2
            continue
        start_line, start_column = line, column
        if text.startswith("->", index):
            tokens.append(Token("ARROW", "->", line, column))
            advance("->")
            index += 2
            continue
        if char in PUNCTUATION:
            tokens.append(Token(PUNCTUATION[char], char, line, column))
            advance(char)
            index += 1
            continue
        if char == '"':
            index += 1
            advance('"')
            value = []
            while index < len(text) and text[index] != '"':
                current = text[index]
                if current == "\\":
                    if index + 1 >= len(text):
                        raise DotSyntaxError(
                            source, start_line, start_column, "unterminated string"
                        )
                    escaped = text[index + 1]
                    replacements = {"n": "\n", "r": "\r", "t": "\t"}
                    value.append(replacements.get(escaped, escaped))
                    advance(text[index:index + 2])
                    index += 2
                    continue
                value.append(current)
                advance(current)
                index += 1
            if index >= len(text):
                raise DotSyntaxError(
                    source, start_line, start_column, "unterminated string"
                )
            advance('"')
            index += 1
            tokens.append(Token("STRING", "".join(value), start_line, start_column))
            continue
        end = index
        while end < len(text):
            if text[end].isspace() or text[end] in '{}[]=,;"':
                break
            if text.startswith("->", end) or text.startswith("//", end):
                break
            end += 1
        if end == index:
            raise DotSyntaxError(source, line, column, f"unexpected character {char!r}")
        value = text[index:end]
        tokens.append(Token("IDENT", value, start_line, start_column))
        advance(value)
        index = end
    tokens.append(Token("EOF", "", line, column))
    return tokens


class Parser:
    def __init__(self, tokens: list[Token], *, source: str, text: str) -> None:
        self.tokens = tokens
        self.source = source
        self.text = text
        self.index = 0

    @property
    def current(self) -> Token:
        return self.tokens[self.index]

    def advance(self) -> Token:
        token = self.current
        self.index += 1
        return token

    def error(self, message: str, token: Token | None = None) -> DotSyntaxError:
        target = token or self.current
        return DotSyntaxError(self.source, target.line, target.column, message)

    def accept(self, kind: str, value: str | None = None) -> Token | None:
        token = self.current
        if token.kind == kind and (value is None or token.value == value):
            return self.advance()
        return None

    def require(self, kind: str, message: str, value: str | None = None) -> Token:
        token = self.accept(kind, value)
        if token is None:
            raise self.error(message)
        return token

    def identifier(self, message: str = "expected identifier") -> Token:
        if self.current.kind not in ("IDENT", "STRING"):
            raise self.error(message)
        return self.advance()

    def attributes(self) -> LocatedAttributes:
        opening = self.require("LBRACKET", "expected '['")
        values: dict[str, str] = {}
        while self.current.kind != "RBRACKET":
            key = self.identifier("expected attribute name")
            self.require("EQUAL", f"expected '=' after {key.value}")
            value = self.identifier(f"expected value for {key.value}")
            if key.value in values:
                raise self.error(f"duplicate attribute {key.value}", key)
            values[key.value] = value.value
            if self.accept("COMMA") or self.accept("SEMI"):
                continue
            if self.current.kind == "RBRACKET":
                break
            if self.current.kind in ("IDENT", "STRING"):
                continue
            raise self.error("expected ',', ';', or ']' after attribute")
        self.require("RBRACKET", "expected ']'")
        return LocatedAttributes(values, opening.line, opening.column)

    def parse(self) -> DotGraph:
        self.accept("IDENT", "strict")
        self.require("IDENT", "expected 'digraph'", "digraph")
        graph_id = "workflow"
        if self.current.kind in ("IDENT", "STRING"):
            graph_id = self.advance().value
        self.require("LBRACE", "expected '{' after graph name")
        graph_attributes: dict[str, str] = {}
        node_defaults: dict[str, str] = {}
        edge_defaults: dict[str, str] = {}
        nodes: dict[str, LocatedAttributes] = {}
        edges: list[DotEdge] = []
        while self.current.kind != "RBRACE":
            if self.current.kind == "EOF":
                raise self.error("expected '}' before end of file")
            if self.accept("SEMI"):
                continue
            first = self.identifier()
            if first.value in ("graph", "node", "edge") and self.current.kind == "LBRACKET":
                attributes = self.attributes().values
                target = {
                    "graph": graph_attributes,
                    "node": node_defaults,
                    "edge": edge_defaults,
                }[first.value]
                target.update(attributes)
            elif self.accept("ARROW"):
                target = self.identifier("expected edge target")
                values = dict(edge_defaults)
                located = LocatedAttributes(values, first.line, first.column)
                if self.current.kind == "LBRACKET":
                    provided = self.attributes()
                    values.update(provided.values)
                    located = LocatedAttributes(values, provided.line, provided.column)
                edges.append(DotEdge(
                    first.value, target.value, located, first.line, first.column
                ))
                if self.current.kind == "ARROW":
                    raise self.error("chained edges are not supported; write each edge explicitly")
            elif self.current.kind == "LBRACKET":
                provided = self.attributes()
                if first.value in nodes:
                    raise self.error(f"duplicate node {first.value}", first)
                nodes[first.value] = LocatedAttributes(
                    dict(provided.values), first.line, first.column
                )
            elif self.accept("EQUAL"):
                value = self.identifier(f"expected value for {first.value}")
                graph_attributes[first.value] = value.value
            else:
                if first.value in nodes:
                    raise self.error(f"duplicate node {first.value}", first)
                nodes[first.value] = LocatedAttributes({}, first.line, first.column)
            self.accept("SEMI")
        self.require("RBRACE", "expected '}'")
        self.accept("SEMI")
        self.require("EOF", "unexpected content after graph")
        return DotGraph(
            graph_id=graph_id,
            attributes=graph_attributes,
            node_defaults=node_defaults,
            edge_defaults=edge_defaults,
            nodes=nodes,
            edges=tuple(edges),
            source_name=self.source,
            source_text=self.text,
        )


def parse_dot(text: str, *, source: str = "<dot>") -> DotGraph:
    return Parser(tokenize(text, source=source), source=source, text=text).parse()


def load_dot(path: str | Path) -> DotGraph:
    resolved = Path(path)
    return parse_dot(resolved.read_text(encoding="utf-8"), source=str(resolved))
