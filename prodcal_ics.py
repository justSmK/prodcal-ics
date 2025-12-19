#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from icalendar import Calendar, Event
from lxml import html
import requests

from datetime import datetime, timedelta
import argparse
import logging
import re


MONTHS_RU = [
    "Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
    "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь",
]

# How many all-day dates to keep in a single VEVENT.
# Long all-day multi-week events are sometimes not shown by Google/Apple for subscribed ICS.
MAX_SPAN_DAYS = 7


def fetch_hh_calendar_page(year: int) -> bytes | None:
    url = f"https://hh.ru/article/calendar{year}"
    logging.info(url)

    headers = {"User-Agent": "curl/7.68.0"}
    resp = requests.get(url, headers=headers, allow_redirects=True, timeout=20)

    if resp.status_code == 404:
        return None

    resp.raise_for_status()
    return resp.content


def _classify_day_type(hint_text: str, li_classes: str) -> tuple[str, str | None] | None:
    """
    Returns (kind, holiday_name)
      - kind: "holiday" | "dayoff" | "shortened"
      - holiday_name: optional (RU text as on HH)
    Or None if it's a normal working day.
    """
    hint_text = (hint_text or "").strip()

    # Prefer class-based shortened marker if present
    if "shortened" in (li_classes or ""):
        return ("shortened", None)

    if hint_text.startswith("Предпраздничный день"):
        return ("shortened", None)

    # HH typically describes non-working days like:
    # "Выходной день"
    # "Выходной день (перенос)"
    # "Выходной день. Новогодние каникулы"
    if hint_text.startswith("Выходной день."):
        name = hint_text.split(".", 1)[1].strip() or None
        return ("holiday", name)

    if hint_text.startswith("Выходной день"):
        return ("dayoff", None)

    return None


def parse_months_old_layout(tree) -> list[dict]:
    """
    Old layout (used for many years): months are blocks with calendar-list__item__title/title,
    inside each month there are li with numbers and optional calendar-hint.
    Returns: list of 12 dicts: {day_int: (kind, holiday_name)}
    """
    months = tree.xpath(
        "//div[@class='calendar-list__item__title' or @class='calendar-list__item-title']/.."
    )
    if len(months) != 12:
        return []

    result: list[dict] = []
    for m in months:
        month_map: dict[int, tuple[str, str | None]] = {}

        # Any calendar day cell:
        # <li class="calendar-list__numbers__item ...">1<div class="calendar-hint">...</div></li>
        lis = m.xpath(".//li[contains(@class,'calendar-list__numbers__item')]")
        for li in lis:
            day_raw = (li.text_content() or "").strip()
            # day_raw contains also hint text; extract the first 1-2 digit number
            mday = re.match(r"^\s*(\d{1,2})\b", day_raw)
            if not mday:
                continue
            day = int(mday.group(1))

            hint = li.xpath(".//div[contains(@class,'calendar-hint')]/text()")
            hint_text = " ".join([t.strip() for t in hint if t.strip()]).strip()

            classes = " ".join(li.xpath("./@class"))
            classified = _classify_day_type(hint_text, classes)
            if classified:
                month_map[day] = classified

        result.append(month_map)

    return result


def parse_months_text_fallback(tree) -> list[dict]:
    """
    Fallback parser for newer HH layout (e.g., when /article/calendarYYYY redirects to /calendar).
    Uses text_content and month headers to slice into 12 chunks.
    Classification uses presence of "Выходной день", "Праздничный ..." isn't always explicit,
    but in HH content we can detect "Выходной день. <name>" vs "Выходной день".
    Returns: list of 12 dicts.
    """
    text = tree.text_content().replace("\xa0", " ")

    # find month starts
    positions = []
    for name in MONTHS_RU:
        positions.append(text.find(name))

    if any(p == -1 for p in positions):
        raise Exception("Could not find all month names in HH page text (layout changed).")

    result: list[dict] = []
    for i in range(12):
        start = positions[i]
        end = positions[i + 1] if i < 11 else len(text)
        chunk = text[start:end]

        month_map: dict[int, tuple[str, str | None]] = {}

        # Find patterns like:
        # "1 Выходной день. Новогодние каникулы"
        # "2 Выходной день"
        # "30 Предпраздничный день, ..."
        # We'll scan for day number followed by one of these markers.
        for m in re.finditer(r"(?:^|\s)(\d{1,2})(?=\s+)", chunk):
            day = int(m.group(1))
            # take a small window after the day number
            window = chunk[m.end(): m.end() + 120].strip()

            kind: tuple[str, str | None] | None = None
            if window.startswith("Предпраздничный день"):
                kind = ("shortened", None)
            elif window.startswith("Выходной день."):
                name = window.split(".", 1)[1].strip()
                # cut at sentence end-ish
                name = re.split(r"[.\n\r]", name, maxsplit=1)[0].strip() or None
                kind = ("holiday", name)
            elif window.startswith("Выходной день"):
                kind = ("dayoff", None)

            if kind:
                month_map[day] = kind

        result.append(month_map)

    return result


def get_days_by_months_with_types(year: int) -> list[dict] | None:
    content = fetch_hh_calendar_page(year)
    if content is None:
        return None

    tree = html.fromstring(content)

    months = parse_months_old_layout(tree)
    if months and len(months) == 12:
        return months

    # Fallback for new layout
    months = parse_months_text_fallback(tree)
    if months and len(months) == 12:
        return months

    raise Exception(f"Could not parse 12 months for year {year} (HH layout changed).")


def group_consecutive_days(days: list[int]) -> list[list[int]]:
    """Group sorted days into consecutive runs."""
    if not days:
        return []
    days = sorted(days)
    groups = [[days[0]]]
    for d in days[1:]:
        if d == groups[-1][-1] + 1:
            groups[-1].append(d)
        else:
            groups.append([d])
    return groups


def split_run_into_chunks(run: list[int], max_len: int) -> list[list[int]]:
    """Split a consecutive run into smaller runs with length <= max_len."""
    if len(run) <= max_len:
        return [run]
    chunks = []
    i = 0
    while i < len(run):
        chunks.append(run[i:i + max_len])
        i += max_len
    return chunks


def make_event(year: int, month: int, day_start: int, day_end: int, summary: str, description: str | None):
    ev = Event()
    ev.add("summary", summary)
    if description:
        ev.add("description", description)

    ev.add("dtstart", datetime(year, month, day_start, 0, 0, 0).date())
    ev.add("dtend", datetime(year, month, day_end, 0, 0, 0).date() + timedelta(days=1))
    ev.add("dtstamp", datetime.utcnow())

    # Short, stable UID (no folding)
    uid = f"ru-prodcal-{year}{month:02d}{day_start:02d}-{day_end:02d}-{summary.lower().replace(' ', '-')}"
    ev.add("uid", uid)

    return ev


def generate_events(year: int, months_maps: list[dict]) -> list[Event]:
    events: list[Event] = []

    for month, month_map in enumerate(months_maps, start=1):
        # Collect days by type
        dayoff_days = [d for d, (k, _) in month_map.items() if k == "dayoff"]
        holiday_days = [d for d, (k, _) in month_map.items() if k == "holiday"]
        shortened_days = [d for d, (k, _) in month_map.items() if k == "shortened"]

        # Helper to create grouped events for a set of days
        def add_grouped(days: list[int], summary: str, desc_lookup: dict[int, str] | None = None):
            for run in group_consecutive_days(days):
                for chunk in split_run_into_chunks(run, MAX_SPAN_DAYS):
                    ds, de = chunk[0], chunk[-1]
                    description = None
                    if desc_lookup:
                        # If all days have the same holiday name, show it; otherwise generic.
                        names = {desc_lookup.get(x) for x in chunk if desc_lookup.get(x)}
                        if len(names) == 1:
                            description = next(iter(names))
                    events.append(make_event(year, month, ds, de, summary, description))

        # For holidays we can optionally keep the RU name in DESCRIPTION
        holiday_desc = {d: (month_map[d][1] or "") for d in holiday_days}

        add_grouped(dayoff_days, "Day off")
        add_grouped(holiday_days, "Holiday", holiday_desc)
        add_grouped(shortened_days, "Shortened workday")

    return events


def parse_args():
    parser = argparse.ArgumentParser(
        description="Fetches HH production calendar and generates an iCalendar (.ics) feed."
    )

    default_output_file = "prodcal.ics"
    parser.add_argument(
        "-o",
        dest="output_file",
        metavar="out",
        default=default_output_file,
        help=f"output file (default: {default_output_file})",
    )

    parser.add_argument(
        "--start-year",
        metavar="yyyy",
        type=int,
        default=datetime.today().year,
        help="year calendar starts (default: current year)",
    )

    parser.add_argument(
        "--end-year",
        metavar="yyyy",
        type=int,
        default=(datetime.today().year + 1),
        help="year calendar ends (default: next year)",
    )

    parser.add_argument("--log-level", metavar="level", default="INFO")
    return parser.parse_args()


def generate_calendar(events: list[Event]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//ru-prodcal-ics//Ru Working Days Calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("NAME", "Ru Working Days Calendar")
    cal.add("X-WR-CALNAME", "Ru Working Days Calendar")
    cal.add("X-WR-CALDESC", "Working days calendar for Russia: day off, holiday, shortened workday (data source: hh.ru).")

    for e in events:
        cal.add_component(e)

    return cal


def setup_logging(log_level: str):
    logging_level = getattr(logging, log_level.upper(), None)
    if not isinstance(logging_level, int):
        raise ValueError(f"Invalid log level: {log_level}")

    logging.basicConfig(
        level=logging_level,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="[%d/%m/%Y:%H:%M:%S %z]",
    )


if __name__ == "__main__":
    args = parse_args()
    setup_logging(args.log_level)

    events: list[Event] = []

    for year in range(args.start_year, args.end_year + 1):
        months_maps = get_days_by_months_with_types(year)
        if not months_maps:
            break
        events.extend(generate_events(year, months_maps))

    cal = generate_calendar(events)

    # Write bytes to keep proper iCalendar line endings/folding as generated by library
    with open(args.output_file, "wb") as f:
        f.write(cal.to_ical())
