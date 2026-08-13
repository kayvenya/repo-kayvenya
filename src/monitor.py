from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from hashlib import sha256
from html import unescape
from html.parser import HTMLParser


TARGET_HEADING = "Записаться на экскурсию"
TARGET_END_MARKER = "Контактная информация"


def normalize_text(value: str) -> str:
    return " ".join(unescape(value).split())


NO_TOURS_TEXT = normalize_text(
    """
    Записаться на экскурсию
    Запись на новые экскурсии открывается ежедневно по будням в 13:00.
    В настоящий момент доступных экскурсий нет
    """
)


class PageKind(Enum):
    NO_TOURS = "no_tours"
    TOURS_AVAILABLE = "tours_available"
    UNEXPECTED_FORMAT = "unexpected_format"


@dataclass(frozen=True)
class PageResult:
    kind: PageKind
    text: str
    fingerprint: str


@dataclass(frozen=True)
class _TextToken:
    text: str
    actionable: bool


class _VisibleTextParser(HTMLParser):
    _IGNORED_TAGS = {"script", "style", "template"}
    _ACTIONABLE_TAGS = {"a", "button", "form", "input", "select"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.tokens: list[_TextToken] = []
        self._ignored_depth = 0
        self._actionable_depth = 0
        self._body_depth = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag = tag.lower()
        if tag == "body":
            self._body_depth += 1
        if tag in self._IGNORED_TAGS:
            self._ignored_depth += 1
        if tag in self._ACTIONABLE_TAGS:
            self._actionable_depth += 1

    def handle_endtag(self, tag: str) -> None:
        tag = tag.lower()
        if tag in self._ACTIONABLE_TAGS and self._actionable_depth:
            self._actionable_depth -= 1
        if tag in self._IGNORED_TAGS and self._ignored_depth:
            self._ignored_depth -= 1
        if tag == "body" and self._body_depth:
            self._body_depth -= 1

    def handle_data(self, data: str) -> None:
        if self._ignored_depth or not self._body_depth:
            return
        text = normalize_text(data)
        if text:
            self.tokens.append(_TextToken(text, self._actionable_depth > 0))


def _fingerprint(text: str) -> str:
    return sha256(text.encode("utf-8")).hexdigest()


def classify_tour_page(source: str) -> PageResult:
    parser = _VisibleTextParser()
    parser.feed(source)
    parser.close()

    start = next(
        (index for index, token in enumerate(parser.tokens) if token.text == TARGET_HEADING),
        None,
    )
    end = None
    if start is not None:
        end = next(
            (
                index
                for index, token in enumerate(parser.tokens[start + 1 :], start + 1)
                if token.text == TARGET_END_MARKER
            ),
            None,
        )

    if start is None or end is None:
        page_text = normalize_text(" ".join(token.text for token in parser.tokens))
        return PageResult(
            PageKind.UNEXPECTED_FORMAT,
            page_text[:800],
            _fingerprint(page_text),
        )

    block = parser.tokens[start:end]
    block_text = normalize_text(" ".join(token.text for token in block))
    if block_text == NO_TOURS_TEXT:
        kind = PageKind.NO_TOURS
    elif any(token.actionable for token in block) and block_text != TARGET_HEADING:
        kind = PageKind.TOURS_AVAILABLE
    else:
        kind = PageKind.UNEXPECTED_FORMAT

    return PageResult(kind, block_text[:800], _fingerprint(block_text))
