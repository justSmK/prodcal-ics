#!/usr/bin/env python3
# -*- coding: utf-8 -*-

from icalendar import Calendar, Event
import requests

from datetime import date, datetime, timedelta, UTC
import argparse
import json
import logging
import xml.etree.ElementTree as ET


MAX_SPAN_DAYS = 10

CALENDAR_URL = "https://raw.githubusercontent.com/xmlcalendar/data/master/ru/{year}/calendar.xml"

DAY_OFF, SHORTENED, WORKING = "1", "2", "3"


def fetch_calendar(year: int) -> bytes | None:
    url = CALENDAR_URL.format(year=year)
    logging.info(url)

    r = requests.get(url, timeout=20)

    if r.status_code == 404:
        logging.warning("No calendar for year %d (404)", year)
        return None

    r.raise_for_status()
    return r.content


def classify_day(node: ET.Element) -> str | None:
    day_type = node.get("t")

    if day_type == DAY_OFF:
        return "holiday" if node.get("h") else "dayoff"

    if day_type == SHORTENED:
        return "shortened"

    if day_type == WORKING:
        return None

    raise RuntimeError(f"Unknown day type {day_type!r} on {node.get('d')}")


def parse_calendar(year: int, content: bytes) -> list[dict[int, str]]:
    root = ET.fromstring(content)

    if root.get("year") != str(year):
        raise RuntimeError(f"Calendar for year {year} declares year {root.get('year')!r}")

    listed: dict[date, str | None] = {}
    for node in root.iterfind("days/day"):
        month, day = (int(part) for part in node.attrib["d"].split("."))
        listed[date(year, month, day)] = classify_day(node)

    months: list[dict[int, str]] = [{} for _ in range(12)]
    day = date(year, 1, 1)
    while day.year == year:
        kind = listed[day] if day in listed else ("dayoff" if day.weekday() >= 5 else None)
        if kind:
            months[day.month - 1][day.day] = kind
        day += timedelta(days=1)

    return months


def parse_year(year: int) -> list[dict] | None:
    content = fetch_calendar(year)
    if content is None:
        return None

    months = parse_calendar(year, content)
    logging.info("Parsed calendar for year %d", year)
    return months


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
            ("shortened", "* Shortened workday"),
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
    p.add_argument("--years-out", help="Write the years covered by the calendar to this file as a JSON list")
    p.add_argument("--log-level", default="INFO")
    args = p.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level.upper(), logging.INFO),
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    events: list[Event] = []
    years: list[int] = []

    for year in range(args.start_year, args.end_year + 1):
        months = parse_year(year)
        if not months:
            logging.info("Stopping at year %d (no data yet)", year)
            break
        events.extend(generate_events(year, months))
        years.append(year)

    cal = build_calendar(events)

    with open(args.o, "wb") as f:
        f.write(cal.to_ical())

    if args.years_out:
        with open(args.years_out, "w", encoding="utf-8") as f:
            json.dump(years, f)


if __name__ == "__main__":
    main()
