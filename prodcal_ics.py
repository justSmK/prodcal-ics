#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from icalendar import Calendar, Event
from lxml import html
import requests

from datetime import datetime, timedelta, UTC
import argparse
import logging
import re


MAX_SPAN_DAYS = 7


def fetch_page(year: int) -> bytes | None:
    url = f"https://hh.ru/article/calendar{year}"
    logging.info(url)

    headers = {"User-Agent": "curl/7.68.0"}
    r = requests.get(url, headers=headers, allow_redirects=True, timeout=20)

    if r.status_code == 404:
        return None

    r.raise_for_status()
    return r.content


def normalize_text(s: str) -> str:
    return re.sub(r"\s+", " ", (s or "").strip())


def classify_day(hint: str) -> str | None:
    """
    Returns one of:
      - 'holiday'
      - 'dayoff'
      - 'shortened'
    or None (working day)
    """
    hint = normalize_text(hint)

    if hint.startswith("Предпраздничный день"):
        return "shortened"

    if hint.startswith("Выходной день."):
        return "holiday"

    if hint.startswith("Выходной день"):
        return "dayoff"

    return None


def parse_year(year: int) -> list[dict] | None:
    content = fetch_page(year)
    if content is None:
        return None

    tree = html.fromstring(content)

    months = tree.xpath(
        "//div[@class='calendar-list__item__title' or @class='calendar-list__item-title']/.."
    )

    if len(months) != 12:
        raise Exception(f"Unexpected HH layout for year {year}")

    result: list[dict] = []

    for m in months:
        month_map: dict[int, str] = {}

        days = m.xpath(".//li[contains(@class,'calendar-list__numbers__item')]")
        for li in days:
            text = normalize_text(li.text_content())
            mday = re.match(r"^(\d{1,2})\b", text)
            if not mday:
                continue

            day = int(mday.group(1))
            hint_nodes = li.xpath(".//div[contains(@class,'calendar-hint')]//text()")
            hint = normalize_text(" ".join(hint_nodes))

            kind = classify_day(hint)
            if kind:
                month_map[day] = kind

        result.append(month_map)

    return result


def group_consecutive(days: list[int]) -> list[list[int]]:
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


def split_chunks(run: list[int]) -> list[list[int]]:
    return [run[i:i + MAX_SPAN_DAYS] for i in range(0, len(run), MAX_SPAN_DAYS)]


def make_event(year, month, d1, d2, summary):
    e = Event()
    e.add("summary", summary)
    e.add("dtstart", datetime(year, month, d1).date())
    e.add("dtend", datetime(year, month, d2).date() + timedelta(days=1))
    e.add("dtstamp", datetime.now(UTC))
    e.add("uid", f"ru-prodcal-{year}{month:02d}{d1:02d}-{d2:02d}-{summary.lower().replace(' ', '-')}")
    return e


def generate_events(year: int, months: list[dict]) -> list[Event]:
    events: list[Event] = []

    for month, days_map in enumerate(months, start=1):
        for kind, summary in [
            ("holiday", "Holiday"),
            ("dayoff", "Day off"),
            ("shortened", "Shortened workday"),
        ]:
            days = [d for d, k in days_map.items() if k == kind]

            for run in group_consecutive(days):
                for chunk in split_chunks(run):
                    events.append(
                        make_event(year, month, chunk[0], chunk[-1], summary)
                    )

    return events


def build_calendar(events: list[Event]) -> Calendar:
    cal = Calendar()
    cal.add("prodid", "-//ru-prodcal-ics//Ru Non-Working Days Calendar//EN")
    cal.add("version", "2.0")
    cal.add("calscale", "GREGORIAN")
    cal.add("method", "PUBLISH")
    cal.add("NAME", "Ru Non-Working Days")
    cal.add("X-WR-CALNAME", "Ru Non-Working Days")

    for e in sorted(events, key=lambda x: x.decoded("dtstart")):
        cal.add_component(e)

    return cal


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--start-year", type=int, default=datetime.today().year)
    p.add_argument("--end-year", type=int, default=datetime.today().year + 1)
    p.add_argument("-o", default="prodcal.ics")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


    events: list[Event] = []

    for year in range(args.start_year, args.end_year + 1):
        months = parse_year(year)
        if not months:
            break
        events.extend(generate_events(year, months))

    cal = build_calendar(events)

    with open(args.o, "wb") as f:
        f.write(cal.to_ical())


if __name__ == "__main__":
    main()
