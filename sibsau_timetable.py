#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = [
#     "requests[socks]>=2.32",
#     "beautifulsoup4>=4.12",
#     "lxml>=5.2",
#     "icalendar>=6.0",
#     "tzdata>=2024.1",
# ]
# ///
"""Парсер расписания СибГУ (timetable.pallada.sibsau.ru) -> .ics в этом же репозитории.

Запускается GitHub Actions раз в 6 часов: скачивает расписание, собирает .ics
и коммитит их, если расписание изменилось. Google Calendar подписан на файлы по ссылке.

    uv run --script sibsau_timetable.py             # собрать и закоммитить
    uv run --script sibsau_timetable.py --dry-run   # показать, что изменилось бы
    uv run --script sibsau_timetable.py --no-git    # только записать файлы
"""

from __future__ import annotations

import argparse
import hashlib
import logging
import re
import shutil
import subprocess
import sys
import time
import tomllib
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

import requests
from bs4 import BeautifulSoup, Tag
from icalendar import Calendar, Event

BASE_URL = "https://timetable.pallada.sibsau.ru/timetable/group/{group_id}"
USER_AGENT = "Mozilla/5.0 (X11; Fedora; Linux x86_64) sibsau-timetable/1.0"

DAY_CLASSES = ["monday", "tuesday", "wednesday", "thursday", "friday", "saturday"]
KNOWN_TYPES = {"Лекция", "Практика", "Лабораторная работа", "Экзамен", "Зачёт", "Консультация"}

HERE = Path(__file__).resolve().parent
log = logging.getLogger("sibsau")


# --------------------------------------------------------------------------------------
# Конфигурация
# --------------------------------------------------------------------------------------


@dataclass
class GroupConfig:
    """Одна группа из config.toml."""

    id: int
    subgroup: int = 0  # 0 = все подгруппы
    name: str = ""  # автоопределяется со страницы
    calendar_name: str = ""  # по умолчанию "СибГУ — <name>"

    @property
    def title(self) -> str:
        return self.name or str(self.id)

    @property
    def calendar_summary(self) -> str:
        return self.calendar_name or f"СибГУ — {self.title}"


@dataclass
class Config:
    """Разобранный config.toml."""

    groups: list[GroupConfig]
    timezone: str = "Asia/Krasnoyarsk"
    semester_start: date | None = None
    semester_end: date | None = None
    week_offset: int = 0  # 1 — поменять местами 1-ю и 2-ю неделю, если парность не сошлась
    subdir: str = "timetable"  # каталог для .ics внутри репозитория
    base_url: str = ""  # https://<логин>.github.io/ — нужен только чтобы напечатать ссылки
    branch: str = "main"

    @property
    def tz(self) -> ZoneInfo:
        return ZoneInfo(self.timezone)


def default_semester(today: date) -> tuple[date, date]:
    """Границы текущего семестра, если они не заданы в конфиге."""
    if today.month >= 8:
        return date(today.year, 9, 1), date(today.year, 12, 31)
    if today.month <= 1:
        return date(today.year - 1, 9, 1), date(today.year, 1, 31)
    return date(today.year, 2, 1), date(today.year, 6, 30)


def load_config(path: Path) -> Config:
    if not path.exists():
        sys.exit(f"Нет файла конфигурации: {path}")

    raw = tomllib.loads(path.read_text(encoding="utf-8"))
    groups = [
        GroupConfig(
            id=int(item["id"]),
            subgroup=int(item.get("subgroup", 0)),
            name=str(item.get("name", "")),
            calendar_name=str(item.get("calendar_name", "")),
        )
        for item in raw.get("groups", [])
    ]
    if not groups:
        sys.exit("В config.toml не задано ни одной группы ([[groups]])")

    timezone = str(raw.get("timezone", "Asia/Krasnoyarsk"))
    start, end = default_semester(datetime.now(ZoneInfo(timezone)).date())
    return Config(
        groups=groups,
        timezone=timezone,
        semester_start=_as_date(raw.get("semester_start")) or start,
        semester_end=_as_date(raw.get("semester_end")) or end,
        week_offset=int(raw.get("week_offset", 0)),
        subdir=str(raw.get("subdir", "timetable")).strip("/"),
        base_url=str(raw.get("base_url", "")).rstrip("/"),
        branch=str(raw.get("branch", "main")),
    )


def _as_date(value: Any) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


# --------------------------------------------------------------------------------------
# Парсинг сайта
# --------------------------------------------------------------------------------------


@dataclass
class Lesson:
    """Одно занятие в сетке двухнедельного цикла."""

    week: int  # 1 или 2
    weekday: int  # 0 = понедельник
    start: str  # "08:00"
    end: str  # "09:35"
    subject: str
    kind: str  # Лекция / Практика / ...
    teacher: str = ""
    room: str = ""
    address: str = ""
    subgroup: int = 0
    note: str = ""


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers["User-Agent"] = USER_AGENT
    return session


def fetch_page(
    group_id: int,
    session: requests.Session | None = None,
    timeout: int = 30,
    retries: int = 3,
) -> str:
    url = BASE_URL.format(group_id=group_id)
    session = session or build_session()
    last: Exception | None = None
    for attempt in range(1, retries + 1):
        try:
            resp = session.get(url, timeout=timeout)
            resp.raise_for_status()
            resp.encoding = resp.encoding or "utf-8"
            return resp.text
        except requests.RequestException as exc:
            last = exc
            log.warning("Группа %s: попытка %s/%s не удалась: %s", group_id, attempt, retries, exc)
            time.sleep(3 * attempt)
    raise RuntimeError(f"Не удалось скачать расписание группы {group_id}: {last}")


def parse_group_name(soup: BeautifulSoup) -> str:
    header = soup.find("h3", class_="text-center") or soup.find("title")
    if header is None:
        return ""
    text = header.get_text(" ", strip=True)
    match = re.search(r"[\"«„]([^\"»“]+)[\"»“]", text)
    if match:
        return match.group(1).strip()
    return text.replace("Расписание", "").replace("группы", "").strip()


def detect_current_week(soup: BeautifulSoup) -> int | None:
    """Сайт подсвечивает активную вкладку текущей недели — используем её, если нашли."""
    for anchor in soup.find_all("a", href=re.compile(r"#week_[12]_tab")):
        parent = anchor.parent
        classes = parent.get("class", []) if isinstance(parent, Tag) else []
        if "active" in classes:
            match = re.search(r"week_([12])_tab", str(anchor.get("href")))
            if match:
                return int(match.group(1))
    for week in (1, 2):
        tab = soup.find(id=f"week_{week}_tab")
        if isinstance(tab, Tag) and "active" in (tab.get("class") or []):
            return week
    return None


def _subject_blocks(line: Tag) -> list[Tag]:
    """Верхнеуровневые блоки предметов внутри строки-пары."""
    row = line.find("div", class_="row")
    if not isinstance(row, Tag):
        return []
    blocks: list[Tag] = []
    for name in row.find_all("span", class_="name"):
        block: Tag = name
        while block.parent is not None and block.parent is not row:
            block = block.parent
        if block.name == "div" and not any(block is seen for seen in blocks):
            blocks.append(block)
    return blocks


def _parse_time(line: Tag) -> tuple[str, str]:
    holder = line.find("div", class_="hidden-xs") or line
    times = re.findall(r"\d{1,2}:\d{2}", holder.get_text(" ", strip=True))
    if len(times) >= 2:
        return times[0].zfill(5), times[1].zfill(5)
    if len(times) == 1:
        start = times[0].zfill(5)
        hh, mm = (int(x) for x in start.split(":"))
        total = hh * 60 + mm + 95
        return start, f"{total // 60 % 24:02d}:{total % 60:02d}"
    return "", ""


def _parse_kind(block: Tag, subject: str) -> str:
    name_span = block.find("span", class_="name")
    holder = name_span.parent if isinstance(name_span, Tag) and name_span.parent else block
    text = holder.get_text(" ", strip=True).replace(subject, " ")
    candidates = re.findall(r"\(([^()]+)\)", text)
    for candidate in reversed(candidates):
        value = candidate.strip()
        if value in KNOWN_TYPES:
            return value
    for known in KNOWN_TYPES:
        if known.lower() in text.lower():
            return known
    return candidates[-1].strip() if candidates else ""


def _parse_room(block: Tag) -> tuple[str, str]:
    anchor = block.find("a", href="#")
    if not isinstance(anchor, Tag):
        return "", ""
    room = re.sub(r"\s+", " ", anchor.get_text(" ", strip=True))
    room = room.replace("корп. ", "").replace(" каб. ", "-").replace('"', "").strip()
    address = str(anchor.get("title", "")).strip()
    return room, address


def _parse_teacher(block: Tag) -> str:
    for anchor in block.find_all("a"):
        href = str(anchor.get("href", ""))
        if "professor" in href or "teacher" in href:
            return anchor.get_text(" ", strip=True)
    for anchor in block.find_all("a"):
        if str(anchor.get("href", "")) != "#":
            return anchor.get_text(" ", strip=True)
    return ""


def _first_digit(text: str) -> int:
    match = re.search(r"\d+", text)
    return int(match.group()) if match else 0


def _parse_subgroup(block: Tag) -> int:
    tag = block.find("li", class_="num_pdgrp")
    if isinstance(tag, Tag):
        return _first_digit(tag.get_text(" ", strip=True))
    if block.find("i", class_="fa-paperclip") is not None:
        items = block.find_all("li")
        if items:
            return _first_digit(items[-1].get_text(" ", strip=True))
    return 0


def parse_timetable(html: str) -> tuple[str, int | None, list[Lesson]]:
    soup = BeautifulSoup(html, "lxml")
    name = parse_group_name(soup)
    current_week = detect_current_week(soup)
    lessons: list[Lesson] = []

    for week in (1, 2):
        for weekday, day_class in enumerate(DAY_CLASSES):
            bodies = soup.select(f"#week_{week}_tab > div.day.{day_class} > div.body")
            if not bodies:
                continue
            for line in bodies[0].find_all("div", class_="line"):
                start, end = _parse_time(line)
                if not start:
                    continue
                for block in _subject_blocks(line):
                    name_span = block.find("span", class_="name")
                    subject = name_span.get_text(" ", strip=True) if name_span else ""
                    if not subject:
                        continue
                    room, address = _parse_room(block)
                    lessons.append(
                        Lesson(
                            week=week,
                            weekday=weekday,
                            start=start,
                            end=end,
                            subject=re.sub(r"\s+", " ", subject),
                            kind=_parse_kind(block, subject),
                            teacher=_parse_teacher(block),
                            room=room,
                            address=address,
                            subgroup=_parse_subgroup(block),
                        )
                    )
    return name, current_week, lessons


# --------------------------------------------------------------------------------------
# Раскладка двухнедельного цикла по датам
# --------------------------------------------------------------------------------------


@dataclass
class CalendarEvent:
    """Конкретное занятие в конкретный день."""

    uid: str
    summary: str
    description: str
    location: str
    start: datetime
    end: datetime

    def signature(self) -> str:
        payload = "|".join(
            [
                self.summary,
                self.description,
                self.location,
                self.start.isoformat(),
                self.end.isoformat(),
            ]
        )
        return hashlib.sha1(payload.encode("utf-8")).hexdigest()[:16]  # noqa: S324


def week_for_date(day: date, current_week: int, anchor: date, offset: int = 0) -> int:
    """Определяет номер недели (1/2) для даты по чётности ISO-недели."""
    delta = day.isocalendar()[1] - anchor.isocalendar()[1]
    week = current_week if delta % 2 == 0 else 3 - current_week
    if offset % 2:
        week = 3 - week
    return week


def daterange(start: date, end: date) -> Iterator[date]:
    day = start
    while day <= end:
        yield day
        day += timedelta(days=1)


def build_events(
    group: GroupConfig,
    lessons: list[Lesson],
    cfg: Config,
    current_week: int,
) -> list[CalendarEvent]:
    assert cfg.semester_start and cfg.semester_end
    tz = cfg.tz
    anchor = datetime.now(tz).date()
    events: list[CalendarEvent] = []

    by_slot: dict[tuple[int, int], list[Lesson]] = {}
    for lesson in lessons:
        if group.subgroup and lesson.subgroup and lesson.subgroup != group.subgroup:
            continue
        by_slot.setdefault((lesson.week, lesson.weekday), []).append(lesson)

    for day in daterange(cfg.semester_start, cfg.semester_end):
        week = week_for_date(day, current_week, anchor, cfg.week_offset)
        for lesson in by_slot.get((week, day.weekday()), []):
            start = datetime.combine(day, _time(lesson.start), tzinfo=tz)
            end = datetime.combine(day, _time(lesson.end), tzinfo=tz)
            if end <= start:
                end = start + timedelta(minutes=95)

            summary = f"{lesson.subject} ({lesson.kind})" if lesson.kind else lesson.subject
            description_parts = [f"Группа: {group.title}"]
            if lesson.teacher:
                description_parts.append(f"Преподаватель: {lesson.teacher}")
            if lesson.subgroup:
                description_parts.append(f"Подгруппа: {lesson.subgroup}")
            if lesson.note:
                description_parts.append(lesson.note)
            description_parts.append(f"Неделя: {week}")

            location = ", ".join(part for part in (lesson.room, lesson.address) if part)
            raw_uid = (
                f"{group.id}|{day.isoformat()}|{lesson.start}|{lesson.subject}|{lesson.subgroup}"
            )
            uid = hashlib.sha1(raw_uid.encode("utf-8")).hexdigest()  # noqa: S324

            events.append(
                CalendarEvent(
                    uid=f"{uid}@sibsau-timetable",
                    summary=summary,
                    description="\n".join(description_parts),
                    location=location,
                    start=start,
                    end=end,
                )
            )
    events.sort(key=lambda e: (e.start, e.summary))
    return events


def _time(value: str) -> dtime:
    hh, mm = (int(part) for part in value.split(":"))
    return dtime(hour=hh, minute=mm)


# --------------------------------------------------------------------------------------
# ICS
# --------------------------------------------------------------------------------------


def render_ics(events: list[CalendarEvent], calendar_name: str, tz: str) -> bytes:
    cal = Calendar()
    cal.add("prodid", "-//sibsau-timetable//RU")
    cal.add("version", "2.0")
    cal.add("x-wr-calname", calendar_name)
    cal.add("x-wr-timezone", tz)
    now = datetime.now(ZoneInfo("UTC"))

    for item in events:
        event = Event()
        event.add("uid", item.uid)
        event.add("dtstamp", now)
        event.add("dtstart", item.start)
        event.add("dtend", item.end)
        event.add("summary", item.summary)
        if item.location:
            event.add("location", item.location)
        if item.description:
            event.add("description", item.description)
        cal.add_component(event)

    if hasattr(cal, "add_missing_timezones"):
        cal.add_missing_timezones()

    return bytes(cal.to_ical())


# --------------------------------------------------------------------------------------
# Публикация в git (GitHub Pages)
# --------------------------------------------------------------------------------------


def strip_volatile(data: bytes) -> bytes:
    """Убирает строки, меняющиеся при каждой генерации, — для сравнения содержимого."""
    keep = [
        line
        for line in data.splitlines()
        if not line.upper().startswith((b"DTSTAMP", b"PRODID", b"LAST-MODIFIED"))
    ]
    return b"\n".join(keep)


def content_differs(data: bytes, path: Path) -> bool:
    """True, если файла нет или его содержимое отличается по существу."""
    if not path.exists():
        return True
    return strip_volatile(path.read_bytes()) != strip_volatile(data)


def write_if_changed(data: bytes, path: Path) -> bool:
    """Пишет файл, только если содержимое изменилось по существу. True — если записали."""
    if not content_differs(data, path):
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return True


def git(*args: str, check: bool = True) -> subprocess.CompletedProcess[str]:
    """Запускает git в каталоге репозитория (это каталог самого скрипта)."""
    exe = shutil.which("git")
    if exe is None:
        sys.exit("git не найден в PATH")
    return subprocess.run(  # noqa: S603
        [exe, "-C", str(HERE), *args],
        check=check,
        capture_output=True,
        text=True,
    )


def git_identity() -> list[str]:
    """В CI git-идентичность не настроена — подставляем свою, не трогая глобальный конфиг."""
    flags: list[str] = []
    name = git("config", "user.name", check=False)
    if name.returncode != 0 or not name.stdout.strip():
        flags += ["-c", "user.name=sibsau-timetable"]
    email = git("config", "user.email", check=False)
    if email.returncode != 0 or not email.stdout.strip():
        flags += ["-c", "user.email=sibsau-timetable@users.noreply.github.com"]
    return flags


def target_dir(cfg: Config) -> Path:
    path = HERE / cfg.subdir if cfg.subdir else HERE
    path.mkdir(parents=True, exist_ok=True)
    return path


def commit_and_push(cfg: Config, changed: list[str]) -> int:
    """Коммитит и пушит изменения. Возвращает код выхода."""
    if not (HERE / ".git").exists():
        sys.exit(f"{HERE} — не git-репозиторий")

    git("add", "-A", "--", cfg.subdir or ".")
    if git("diff", "--cached", "--quiet", check=False).returncode == 0:
        log.info("Изменений в репозитории нет, коммит не нужен")
        return 0

    stamp = datetime.now(cfg.tz).strftime("%Y-%m-%d %H:%M")
    git(*git_identity(), "commit", "-m", f"Расписание: обновление {stamp}")
    log.info("Коммит создан (%s)", ", ".join(changed))

    result = git("push", "origin", f"HEAD:{cfg.branch}", check=False)
    if result.returncode != 0:
        # чаще всего кто-то запушил параллельно — подтягиваем и пробуем ещё раз
        log.warning("push не прошёл, пробую rebase: %s", (result.stderr or "").strip())
        git(*git_identity(), "pull", "--rebase", "origin", cfg.branch, check=False)
        result = git("push", "origin", f"HEAD:{cfg.branch}", check=False)
    if result.returncode != 0:
        log.error("git push не удался: %s", (result.stderr or result.stdout).strip())
        return 1
    log.info("Изменения отправлены в origin/%s", cfg.branch)
    return 0


def subscription_url(cfg: Config, group: GroupConfig) -> str:
    parts = [cfg.base_url or "<base_url не задан>"]
    if cfg.subdir:
        parts.append(cfg.subdir)
    parts.append(ics_name(group))
    return "/".join(parts)


def ics_name(group: GroupConfig) -> str:
    """Имя файла — только ASCII, чтобы ссылка не требовала экранирования."""
    suffix = f"-sub{group.subgroup}" if group.subgroup else ""
    return f"{group.id}{suffix}.ics"


def run(cfg: Config, args: argparse.Namespace) -> int:
    target = target_dir(cfg)
    session = build_session()
    changed: list[str] = []

    for group in cfg.groups:
        html = fetch_page(group.id, session)
        name, current_week, lessons = parse_timetable(html)
        if name and not group.name:
            group.name = name
        if current_week is None:
            current_week = 1 if datetime.now(cfg.tz).date().isocalendar()[1] % 2 else 2
            log.info("Активная вкладка не найдена, беру неделю %s по чётности ISO", current_week)
        if not lessons:
            log.warning("Группа %s: занятий не найдено — проверьте id", group.id)

        events = build_events(group, lessons, cfg, current_week)
        data = render_ics(events, group.calendar_summary, cfg.timezone)
        path = target / ics_name(group)

        if args.dry_run:
            if content_differs(data, path):
                changed.append(path.name)
            log.info(
                "%s: %s событий%s",
                path.name,
                len(events),
                " — обновился бы" if changed and changed[-1] == path.name else "",
            )
            continue

        if write_if_changed(data, path):
            changed.append(path.name)
            log.info("Обновлён %s (%s событий)", path.name, len(events))
        else:
            log.info("Без изменений: %s (%s событий)", path.name, len(events))
        log.info("Ссылка для подписки: %s", subscription_url(cfg, group))

    if not changed:
        log.info("Публиковать нечего")
        return 0
    if args.dry_run:
        log.info("[dry-run] закоммитил бы: %s", ", ".join(changed))
        return 0
    if args.no_git:
        log.info("--no-git: файлы записаны, коммит пропущен")
        return 0
    return commit_and_push(cfg, changed)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Расписание СибГУ -> .ics в этом репозитории")
    parser.add_argument("--config", type=Path, default=HERE / "config.toml")
    parser.add_argument(
        "--dry-run", action="store_true", help="показать изменения, ничего не писать"
    )
    parser.add_argument("--no-git", action="store_true", help="записать файлы без коммита")
    parser.add_argument("--verbose", "-v", action="store_true")

    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    return run(load_config(args.config), args)


if __name__ == "__main__":
    raise SystemExit(main())
