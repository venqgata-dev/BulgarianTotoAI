"""HTML parsing for info.toto.bg result pages.

Selectors are grounded in the live markup inspected in July 2026 and an
Internet Archive snapshot from May 2024 (identical structure, currency label
"лева" instead of "euro"):

    <div class="tir_result">
      <h2 class="tir_title"> Тираж 55 - 16.07.2026 </h2>
      <div class="tir_numbers"> ... <span class="ball-white">5</span> ... </div>
      <div class="tir_jackpot"> ... <div class="... sum ...">2 724 436.24
          <span class="small">euro</span></div> ... </div>
      <div class="tir_pechalbi"><table> ... tier rows ... </table></div>
    </div>

The sidebar repeats ball numbers for every game, so parsing is strictly
scoped to ``div.tir_result``.

Two drawings per session
-------------------------
Both captured pages also show the "Тото 2 - 5 от 35" game (in its sidebar
widget, identically structured in the 2024 and 2026 captures) publishing
**two** independent groups of winning numbers for the same draw session,
each preceded by a ``span.win-numbers`` label reading "1 Теглене" / "2
Теглене" ("I-во"/"II-ро теглене" colloquially). A single-drawing page (e.g.
6/49) instead labels its numbers with a plain "Печеливши числа" text node,
never a ``span.win-numbers``. ``TotoParser.parse_draw_page`` therefore
returns **one :class:`ParsedDraw` per drawing found**, keyed by that label;
pages without a drawing label yield a single-element list with
``drawing=1``.
"""

from __future__ import annotations

import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation

from bs4 import BeautifulSoup, Tag

from app.models.domain import ParsedDraw, ParsedPrizeTier

_TITLE_RE = re.compile(r"Тираж\s+(?P<number>\d+)\s*-\s*(?P<date>\d{2}\.\d{2}\.\d{4})")
_DRAW_URL_RE = re.compile(r"/results/(?P<code>[0-9a-z]+)/(?P<year>\d{4})-(?P<number>\d+)/?$")
_MATCH_COUNT_RE = re.compile(r"(\d+)")
_DRAWING_LABEL_RE = re.compile(r"(?P<drawing>\d+)\s*Теглене", re.IGNORECASE)

_CURRENCY_MAP = (
    (("лев", "лв"), "BGN"),
    (("euro", "евро", "eur"), "EUR"),
)


class ParseError(Exception):
    """Raised when a page does not contain the expected result markup."""


def _normalise_currency(label: str | None) -> str | None:
    if not label:
        return None
    lowered = label.strip().lower()
    for needles, code in _CURRENCY_MAP:
        if any(n in lowered for n in needles):
            return code
    return None


def _parse_amount(text: str) -> Decimal | None:
    """Parse "2 724 436.24" (spaces or NBSP as thousand separators)."""
    cleaned = re.sub(r"[^\d.,]", "", text)
    if not cleaned:
        return None
    cleaned = cleaned.replace(",", ".")
    try:
        return Decimal(cleaned)
    except InvalidOperation:
        return None


def _parse_int(text: str) -> int | None:
    cleaned = re.sub(r"[^\d]", "", text)
    return int(cleaned) if cleaned else None


def _amount_and_currency(tag: Tag) -> tuple[Decimal | None, str | None]:
    currency_tag = tag.find("span", class_="small")
    currency = _normalise_currency(currency_tag.get_text() if currency_tag else None)
    amount = _parse_amount(tag.get_text().replace(currency_tag.get_text(), "") if currency_tag else tag.get_text())
    return amount, currency


class TotoParser:
    """Parses result pages and draw archive links for one or more games."""

    def parse_draw_page(self, html: str, game_code: str, source_url: str = "") -> list[ParsedDraw]:
        """Parse a draw detail (or results list) page into one or more
        :class:`ParsedDraw` (one per drawing found on the page - see the
        module docstring).

        Raises :class:`ParseError` when the result block is missing or
        essential fields cannot be extracted.
        """
        soup = BeautifulSoup(html, "lxml")
        root = soup.find("div", class_="tir_result")
        if not isinstance(root, Tag):
            raise ParseError(f"No div.tir_result found (url={source_url or 'n/a'})")

        title_tag = root.find(class_="tir_title")
        if not isinstance(title_tag, Tag):
            raise ParseError(f"No .tir_title in result block (url={source_url or 'n/a'})")
        match = _TITLE_RE.search(title_tag.get_text())
        if not match:
            raise ParseError(f"Unrecognised draw title {title_tag.get_text()!r}")
        draw_number = int(match.group("number"))
        try:
            draw_date = datetime.strptime(match.group("date"), "%d.%m.%Y").date()
        except ValueError as exc:
            raise ParseError(f"Invalid draw date in title {title_tag.get_text()!r}") from exc

        drawings = self._parse_numbers(root, source_url)
        jackpot, jackpot_currency = self._parse_jackpot(root)
        tiers = self._parse_prize_tiers(root)
        currency = jackpot_currency or next((t.currency for t in tiers if t.currency), None)

        return [
            ParsedDraw(
                game_code=game_code,
                draw_number=draw_number,
                draw_year=draw_date.year,
                draw_date=draw_date,
                drawing=drawing_number,
                numbers=main,
                bonus_numbers=bonus,
                jackpot_amount=jackpot,
                currency=currency,
                prize_tiers=tiers,
                source_url=source_url,
            )
            for drawing_number, (main, bonus) in sorted(drawings.items())
        ]

    @staticmethod
    def _parse_numbers(root: Tag, source_url: str) -> dict[int, tuple[tuple[int, ...], tuple[int, ...]]]:
        container = root.find("div", class_="tir_numbers")
        if not isinstance(container, Tag):
            raise ParseError(f"No .tir_numbers block (url={source_url or 'n/a'})")
        drawings: dict[int, tuple[list[int], list[int]]] = {}
        current_drawing = 1
        for span in container.find_all("span"):
            classes = [str(c) for c in (span.get("class") or [])]
            if "win-numbers" in classes:
                label_match = _DRAWING_LABEL_RE.search(span.get_text())
                if label_match:
                    current_drawing = int(label_match.group("drawing"))
                continue
            if not any(c.startswith("ball") for c in classes):
                continue
            value = _parse_int(span.get_text())
            if value is None:
                raise ParseError(f"Non-numeric ball {span.get_text()!r}")
            main, bonus = drawings.setdefault(current_drawing, ([], []))
            # ball-white = main numbers; any other ball-* colour would be a
            # bonus ball (none of the current games has one).
            (main if "ball-white" in classes else bonus).append(value)
        if not drawings:
            raise ParseError(f"No winning numbers found (url={source_url or 'n/a'})")
        return {
            drawing_number: (tuple(main), tuple(bonus))
            for drawing_number, (main, bonus) in drawings.items()
        }

    @staticmethod
    def _parse_jackpot(root: Tag) -> tuple[Decimal | None, str | None]:
        block = root.find("div", class_="tir_jackpot")
        if not isinstance(block, Tag):
            return None, None
        sum_tag = block.find(class_="sum")
        if not isinstance(sum_tag, Tag):
            return None, None
        return _amount_and_currency(sum_tag)

    @staticmethod
    def _parse_prize_tiers(root: Tag) -> list[ParsedPrizeTier]:
        block = root.find("div", class_="tir_pechalbi")
        if not isinstance(block, Tag):
            return []
        tiers: list[ParsedPrizeTier] = []
        for row in block.find_all("tr"):
            cells = row.find_all("td")
            if len(cells) < 4:
                continue  # header row uses <th>
            label = " ".join(cells[0].get_text().split())
            count_match = _MATCH_COUNT_RE.search(label)
            prize_amount, prize_currency = _amount_and_currency(cells[2])
            total_amount, total_currency = _amount_and_currency(cells[3])
            tiers.append(
                ParsedPrizeTier(
                    label=label,
                    match_count=int(count_match.group(1)) if count_match else None,
                    winners=_parse_int(cells[1].get_text()),
                    prize_amount=prize_amount,
                    total_amount=total_amount,
                    currency=prize_currency or total_currency,
                )
            )
        return tiers

    def parse_draw_links(self, html: str, game_code: str) -> list[str]:
        """Extract draw detail URLs for ``game_code`` from a results list page.

        Returns absolute URLs, most recent first (page order), deduplicated.
        """
        soup = BeautifulSoup(html, "lxml")
        seen: set[str] = set()
        links: list[str] = []
        for anchor in soup.find_all("a", href=True):
            href = str(anchor["href"])
            match = _DRAW_URL_RE.search(href)
            if not match or match.group("code") != game_code:
                continue
            absolute = href if href.startswith("http") else f"https://info.toto.bg{href}"
            if absolute not in seen:
                seen.add(absolute)
                links.append(absolute)
        return links

    @staticmethod
    def draw_ref_from_url(url: str) -> tuple[int, int] | None:
        """Return ``(year, number)`` parsed from a draw detail URL, if present."""
        match = _DRAW_URL_RE.search(url)
        if not match:
            return None
        return int(match.group("year")), int(match.group("number"))
