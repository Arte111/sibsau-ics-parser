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
"""Оффлайн-проверка парсера на фикстуре: uv run --script test_parser.py"""

from __future__ import annotations

import tempfile
from datetime import date
from pathlib import Path

from icalendar import Calendar

from sibsau_timetable import (
    Config,
    GroupConfig,
    build_events,
    ics_name,
    parse_timetable,
    render_ics,
    strip_volatile,
    week_for_date,
    write_if_changed,
)

FIXTURE = """
<html><body>
<h3 class="text-center">Расписание группы "БПИ23-01"</h3>
<ul class="nav nav-tabs">
  <li class="active"><a href="#week_1_tab">I неделя</a></li>
  <li><a href="#week_2_tab">II неделя</a></li>
</ul>
<div class="tab-content">
  <div id="week_1_tab" class="tab-pane active">
    <div class="day monday">
      <div class="head">Понедельник</div>
      <div class="body">
        <div class="line">
          <div class="num">1 пара</div>
          <div class="hidden-xs"><div>08:00</div><div>09:35</div></div>
          <div class="row">
            <div class="col-md-12">
              <ul class="list-unstyled">
                <li><span class="name">Математический анализ</span> (Лекция)</li>
                <li><a href="/timetable/professor/1234">Иванов И. И.</a></li>
                <li><a href="#" title="пр. им. газеты Красноярский рабочий, 31">
                    корп. "Л" каб. 305</a></li>
              </ul>
            </div>
          </div>
        </div>
        <div class="line">
          <div class="num">2 пара</div>
          <div class="hidden-xs"><div>09:45</div><div>11:20</div></div>
          <div class="row">
            <div class="col-md-6">
              <ul class="list-unstyled">
                <li><span class="name">Программирование</span> (Лабораторная работа)</li>
                <li><a href="/timetable/professor/99">Петров П. П.</a></li>
                <li><a href="#" title="ул. Мира, 1">корп. "А" каб. 12</a></li>
                <li class="num_pdgrp"><i class="fa fa-paperclip"></i> подгруппа 1</li>
              </ul>
            </div>
            <div class="col-md-6">
              <ul class="list-unstyled">
                <li><span class="name">Программирование</span> (Лабораторная работа)</li>
                <li><a href="/timetable/professor/98">Сидоров С. С.</a></li>
                <li><a href="#" title="ул. Мира, 1">корп. "А" каб. 14</a></li>
                <li class="num_pdgrp"><i class="fa fa-paperclip"></i> подгруппа 2</li>
              </ul>
            </div>
          </div>
        </div>
      </div>
    </div>
    <div class="day tuesday"><div class="head">Вторник</div></div>
  </div>
  <div id="week_2_tab" class="tab-pane">
    <div class="day wednesday">
      <div class="head">Среда</div>
      <div class="body">
        <div class="line">
          <div class="num">3 пара</div>
          <div class="hidden-xs"><div>11:30</div><div>13:05</div></div>
          <div class="row">
            <div class="col-md-12">
              <div class="inner">
                <ul class="list-unstyled">
                  <li><span class="name">Физическая культура</span> (Практика)</li>
                  <li><a href="#" title="Спорткомплекс">корп. "С" каб. 1</a></li>
                </ul>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</div>
</body></html>
"""


def test_parse() -> list:
    name, current_week, lessons = parse_timetable(FIXTURE)
    assert name == "БПИ23-01", name
    assert current_week == 1, current_week
    assert len(lessons) == 4, lessons

    first = lessons[0]
    assert first.week == 1 and first.weekday == 0
    assert (first.start, first.end) == ("08:00", "09:35")
    assert first.subject == "Математический анализ"
    assert first.kind == "Лекция"
    assert first.teacher == "Иванов И. И."
    assert first.room == "Л-305", first.room
    assert first.address.startswith("пр. им. газеты")
    assert first.subgroup == 0

    lab1, lab2 = lessons[1], lessons[2]
    assert (lab1.subgroup, lab2.subgroup) == (1, 2), (lab1.subgroup, lab2.subgroup)
    assert lab1.room == "А-12" and lab2.room == "А-14"

    # предмет, завёрнутый в дополнительный div, и занятие без преподавателя
    pe = lessons[3]
    assert pe.week == 2 and pe.weekday == 2
    assert pe.subject == "Физическая культура"
    assert pe.kind == "Практика"
    assert pe.teacher == ""
    print("parse: ok")
    return lessons


def test_weeks() -> None:
    anchor = date(2026, 9, 1)  # ISO-неделя 36
    assert week_for_date(anchor, 1, anchor) == 1
    assert week_for_date(date(2026, 9, 8), 1, anchor) == 2
    assert week_for_date(date(2026, 9, 15), 1, anchor) == 1
    assert week_for_date(date(2026, 9, 8), 1, anchor, offset=1) == 1
    print("weeks: ok")


def test_events(lessons: list) -> None:
    cfg = Config(
        groups=[],
        semester_start=date(2026, 9, 1),
        semester_end=date(2026, 9, 30),
    )
    group = GroupConfig(id=15273, name="БПИ23-01", subgroup=1)
    events = build_events(group, lessons, cfg, current_week=1)

    assert events, "события не сгенерированы"
    # подгруппа 2 отфильтрована
    assert all("Подгруппа: 2" not in e.description for e in events)
    # все события внутри окна семестра
    assert all(cfg.semester_start <= e.start.date() <= cfg.semester_end for e in events)
    # uid стабилен между запусками
    again = build_events(group, lessons, cfg, current_week=1)
    assert [e.uid for e in events] == [e.uid for e in again]
    assert [e.signature() for e in events] == [e.signature() for e in again]
    assert str(events[0].start.tzinfo) == "Asia/Krasnoyarsk"

    data = render_ics(events, "СибГУ — БПИ23-01", cfg.timezone)
    cal = Calendar.from_ical(data)
    components = [c for c in cal.walk() if c.name == "VEVENT"]
    assert len(components) == len(events)
    summaries = {str(c["SUMMARY"]) for c in components}
    assert "Математический анализ (Лекция)" in summaries, summaries
    assert "Физическая культура (Практика)" in summaries, summaries
    print(f"events: ok ({len(events)} событий, {len(components)} в .ics)")


def test_publish_helpers(lessons: list) -> None:
    cfg = Config(
        groups=[],
        semester_start=date(2026, 9, 1),
        semester_end=date(2026, 9, 30),
    )
    group = GroupConfig(id=15273, name="БПИ23-01", subgroup=1)
    assert ics_name(group) == "15273-sub1.ics"
    assert ics_name(GroupConfig(id=42)) == "42.ics"

    events = build_events(group, lessons, cfg, current_week=1)
    first = render_ics(events, "СибГУ — БПИ23-01", cfg.timezone)
    second = render_ics(events, "СибГУ — БПИ23-01", cfg.timezone)
    # DTSTAMP меняется каждый запуск, но по существу файл тот же
    assert strip_volatile(first) == strip_volatile(second)

    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "out.ics"
        assert write_if_changed(first, path) is True
        assert write_if_changed(second, path) is False, "повторная запись без изменений"
        changed = render_ics(events[:-1], "СибГУ — БПИ23-01", cfg.timezone)
        assert write_if_changed(changed, path) is True
    print("publish: ok")


if __name__ == "__main__":
    parsed = test_parse()
    test_weeks()
    test_events(parsed)
    test_publish_helpers(parsed)
    print("Все проверки пройдены")
