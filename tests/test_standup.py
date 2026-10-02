"""The daily standup (V1.6): deterministic, from Linear data and the work-request store. No model."""

import asyncio
import re
from datetime import UTC, date, datetime

import pytest

from cycle_runner import standup, telegram_adapter
from cycle_runner.standup import TIMEZONE, collect, previous_weekday, render
from cycle_runner.work_requests import WorkRequestStore

FRIDAY_9AM = datetime(2026, 10, 2, 9, 0, tzinfo=TIMEZONE)
MONDAY_9AM = datetime(2026, 10, 5, 9, 0, tzinfo=TIMEZONE)
URL = "https://linear.app/silverbeer/issue/"


def issue(identifier, title, **fields):
    return {"identifier": identifier, "title": title, "url": f"{URL}{identifier}", **fields}


def linear_data(*, done=(), started=(), gated=(), cycle=True):
    return {
        "teams": {"nodes": [{"activeCycle": {
            "number": 10, "startsAt": "2026-09-27T04:00:00.000Z", "endsAt": "2026-10-04T04:00:00.000Z",
            "progress": 0.5447,
            "issues": {"nodes": [
                {"identifier": "SB-1", "estimate": 3, "state": {"type": "completed"}},
                {"identifier": "SB-2", "estimate": 5, "state": {"type": "started"}},
                {"identifier": "SB-3", "estimate": 2, "state": {"type": "canceled"}},  # not counted
                {"identifier": "SB-4", "estimate": None, "state": {"type": "unstarted"}},
            ]},
        } if cycle else None}]},
        "done": {"nodes": list(done)},
        "started": {"nodes": list(started)},
        "gated": {"nodes": list(gated)},
    }


def started(identifier, title, started_at, blocked_by=None):
    relations = [{"type": "blocks", "issue": {"identifier": b, "state": {"type": t}}} for b, t in (blocked_by or [])]
    return issue(identifier, title, startedAt=started_at, inverseRelations={"nodes": relations})


class FakeLinear:
    def __init__(self, data):
        self.data, self.calls = data, []

    async def query(self, document, variables):
        self.calls.append(variables)
        return self.data


def run(data, now=FRIDAY_9AM, store=None):
    linear = FakeLinear(data)
    return asyncio.run(collect(linear.query, team="SB", now=now, store=store)), linear


# --- when ------------------------------------------------------------------------------


@pytest.mark.parametrize(("today", "expected"), [
    (date(2026, 10, 2), date(2026, 10, 1)),  # Friday -> Thursday
    (date(2026, 10, 5), date(2026, 10, 2)),  # Monday -> Friday
    (date(2026, 10, 4), date(2026, 10, 2)),  # Sunday -> Friday
])
def test_done_counts_from_the_previous_working_day(today, expected):
    assert previous_weekday(today) == expected


def test_linear_is_asked_from_midnight_eastern_of_the_previous_working_day():
    _, linear = run(linear_data(), now=MONDAY_9AM)

    assert linear.calls == [{"team": "SB", "since": "2026-10-02T00:00:00-04:00"}]


# --- what it says --------------------------------------------------------------------------


def test_cycle_progress_counts_points_without_canceled_work():
    result, _ = run(linear_data())

    c = result.cycle
    assert (c.number, c.day, c.days, c.days_left, c.percent) == (10, 6, 7, 2, 54)
    assert (c.points_done, c.points_total) == (3, 8)


def test_in_progress_blocked_and_stuck_are_separated_and_stuck_is_oldest_first():
    result, _ = run(linear_data(started=[
        started("SB-10", "Fresh work", "2026-10-01T14:00:00Z"),
        started("SB-11", "Old work", "2026-06-01T14:00:00Z"),
        started("SB-14", "Older work", "2026-05-01T14:00:00Z"),
        started("SB-15", "A week exactly", "2026-09-25T14:00:00Z"),
        started("SB-12", "Stuck work", "2026-09-30T14:00:00Z", blocked_by=[("SB-9", "started")]),
        started("SB-13", "Unblocked now", "2026-10-02T13:00:00Z", blocked_by=[("SB-8", "completed")]),
    ]))

    assert [(i.id, i.days) for i in result.in_progress] == [("SB-13", 0), ("SB-10", 1), ("SB-15", 7)]
    assert [(i.id, i.days) for i in result.stuck] == [("SB-14", 154), ("SB-11", 123)]
    assert [(i.id, i.note, i.days) for i in result.blocked] == [("SB-12", "blocked by SB-9", 2)]


def gated(identifier, title, label, added_at, created_at="2026-09-01T12:00:00Z"):
    history = [{"createdAt": "2026-09-10T12:00:00Z", "addedLabels": [{"name": "feature"}]},
               {"createdAt": added_at, "addedLabels": [{"name": label}]}]
    return issue(identifier, title, estimate=3, createdAt=created_at, labels={"nodes": [{"name": label}]},
                 history={"nodes": history})


def test_done_is_newest_first_with_points_and_waiting_is_aged_from_the_label():
    result, _ = run(linear_data(
        done=[issue("SB-20", "Earlier", estimate=2, completedAt="2026-10-01T15:00:00Z"),
              issue("SB-21", "Later", estimate=None, completedAt="2026-10-02T12:00:00Z")],
        gated=[gated("SB-31", "Approve me", "gate:awaiting-approval", "2026-10-01T15:00:00Z"),
               gated("SB-30", "Look at me", "gate:needs-human", "2026-09-26T15:00:00Z")],
    ))

    assert [(i.id, i.points) for i in result.done] == [("SB-21", None), ("SB-20", 2)]
    assert [(i.id, i.note, i.days) for i in result.waiting_on_you] == [
        ("SB-30", "needs you", 6), ("SB-31", "awaiting your approval", 1)]


def test_a_gate_without_label_history_is_aged_from_the_issue():
    result, _ = run(linear_data(gated=[
        issue("SB-30", "Old gate", createdAt="2026-09-22T12:00:00Z",
              labels={"nodes": [{"name": "gate:needs-human"}]}, history={"nodes": []})]))

    assert result.waiting_on_you[0].days == 10


def _work_request(store, n, title="Add greet()"):
    request, _ = store.create_for_approval(
        recommendation_id=f"rec-{n}", issue_id=f"SB-{n}", approved_by="telegram:1", approved_at=datetime.now(UTC),
        cycle_number=10, title_at_approval=title, rationale="r", project_id="MT",
    )
    store.claim(request.work_request_id, "test")
    store.start(request.work_request_id)
    return request.work_request_id


def test_cycle_runner_commits_awaiting_review_and_failed_runs_come_from_the_store(work_request_db):
    store = WorkRequestStore(work_request_db)
    ready = _work_request(store, 100, "Ready to review")
    store.finish(ready, "changed", "done", branch="cycle-runner/WR-000001", commit_sha="abc1234def5678" + "0" * 26)
    failed = _work_request(store, 101, "Broke")
    store.finish(failed, "failed", "tests failed")
    quiet = _work_request(store, 102, "Nothing to do")
    store.finish(quiet, "no_change", "already done")

    result, _ = run(linear_data(), now=datetime.now(TIMEZONE), store=store)

    assert [(i.id, i.title, i.note, i.days) for i in result.waiting_on_you] == [
        (ready, "SB-100 Ready to review", "commit abc1234 to review", 0)]
    assert [(i.id, i.note) for i in result.blocked] == [(failed, "run failed")]


def test_no_active_cycle_still_gives_a_standup():
    result, _ = run(linear_data(cycle=False))

    assert result.cycle is None
    assert "No active cycle." in render(result)


def test_a_linear_failure_is_not_turned_into_an_empty_standup():
    async def down(document, variables):
        raise standup.LinearError("could not reach Linear (ConnectError)")

    with pytest.raises(standup.LinearError):
        asyncio.run(collect(down, team="SB", now=FRIDAY_9AM))


# --- how it reads ----------------------------------------------------------------------------


def _full_standup():
    result, _ = run(linear_data(
        done=[issue(f"SB-{n}", f"Closed thing number {n}", estimate=n % 4 or None,
                    completedAt=f"2026-10-02T1{n % 10}:00:00Z") for n in range(40, 50)],
        started=[started("SB-10", "A very long title that goes on and on, well past what used to be cut, "
                         "and edits docs/VERSIONS.md", "2026-10-01T14:00:00Z"),
                 started("SB-11", "Old work", "2026-06-01T14:00:00Z"),
                 started("SB-12", "Stuck <work> & more", "2026-09-30T14:00:00Z", blocked_by=[("SB-9", "started")])],
        gated=[gated("SB-30", "Look at me", "gate:needs-human", "2026-09-26T15:00:00Z"),
               gated("SB-31", "Approve me", "gate:awaiting-approval", "2026-10-01T15:00:00Z")],
    ))
    return render(result)


def link(n):
    return f'<a href="{URL}SB-{n}">SB-{n}</a>'


def test_the_rendered_standup_lists_everything_in_full():
    done = [(49, "1 pt"), (48, None), (47, "3 pts"), (46, "2 pts"), (45, "1 pt"), (44, None), (43, "3 pts"),
            (42, "2 pts"), (41, "1 pt"), (40, None)]
    assert _full_standup() == "\n".join([
        "☀️ <b>Standup · Fri 2 Oct</b>",
        "<b>Cycle 10</b> · day 6 of 7 · 2 days left",
        "<code>▓▓▓▓▓░░░░░</code> 54% · 3 of 8 pts",
        "",
        "🚨 <b>Needs attention</b>",
        f"• 1 issue stuck in progress; oldest {link(11)}, <b>123 days</b>",
        f"• 1 item waiting on you over 2 days; longest {link(30)}, <b>6 days</b>",
        "• 1 blocked",
        "",
        "✅ <b>Done since yesterday</b> · 10 issues · 13 pts",
        *[f"• {link(n)} Closed thing number {n} · " + (f"<b>{pts}</b>" if pts else "<i>no estimate</i>")
          for n, pts in done],
        "",
        "🔨 <b>In progress</b> · 1",
        f"• {link(10)} A very long title that goes on and on, well past what used to be cut, "
        "and edits <code>docs/VERSIONS.md</code> · 1 day",
        "",
        "🐢 <b>Stuck in progress</b> (&gt;7 days) · 1",
        f"• {link(11)} Old work · <b>123 days</b>",
        "",
        "⛔ <b>Blocked</b> · 1",
        f"• {link(12)} Stuck &lt;work&gt; &amp; more · blocked by SB-9 · 2 days",
        "",
        "⏳ <b>Waiting on you</b> · 2",
        f"• {link(30)} Look at me · needs you · 🔴 <b>waiting 6 days</b>",
        f"• {link(31)} Approve me · awaiting your approval · waiting 1 day",
    ])


def test_nothing_stuck_or_waiting_long_means_no_alert():
    result, _ = run(linear_data(started=[started("SB-10", "Fresh", "2026-10-01T14:00:00Z")]))
    assert "Needs attention" not in render(result)


def test_monday_says_since_friday_and_empty_sections_say_so():
    result, _ = run(linear_data(), now=MONDAY_9AM)
    text = render(result)

    assert "✅ <b>Done since Friday</b> · 0 issues · 0 pts\n<i>Nothing closed.</i>" in text
    assert "🔨 <b>In progress</b> · 0\n<i>Nothing in progress.</i>" in text
    assert "Blocked" not in text and "Waiting on you" not in text and "Stuck" not in text


# --- sending -----------------------------------------------------------------------------------


def test_send_html_sends_once_per_chat_without_link_previews(monkeypatch):
    sent = []

    class FakeBot:
        def __init__(self, token):
            assert token == "t0ken"

        async def __aenter__(self):
            return self

        async def __aexit__(self, *exc):
            return False

        async def send_message(self, chat_id, text, **kwargs):
            sent.append((chat_id, text, kwargs["parse_mode"], kwargs["link_preview_options"].is_disabled))

    monkeypatch.setattr(telegram_adapter, "Bot", FakeBot)

    asyncio.run(telegram_adapter.send_html("t0ken", [1, 2], "<b>hi</b>"))

    assert sent == [(1, "<b>hi</b>", "HTML", True), (2, "<b>hi</b>", "HTML", True)]


def test_a_long_standup_is_split_between_lines_never_inside_one():
    lines = [f'• <a href="{URL}SB-{n}">SB-{n}</a> ' + "word " * 30 for n in range(200)]
    text = "☀️ <b>Standup</b>\n\n✅ <b>Done</b>\n" + "\n".join(lines)

    chunks = telegram_adapter.split_lines(text, limit=4096)

    assert len(chunks) > 1 and all(len(c) <= 4096 for c in chunks)
    assert "\n".join(chunks).replace("\n\n", "\n") == text.replace("\n\n", "\n")
    assert all(line in lines or line.startswith(("☀️", "✅")) or not line
               for chunk in chunks for line in chunk.split("\n"))


def test_without_send_the_command_prints_and_never_touches_telegram(monkeypatch, capsys):
    monkeypatch.setattr(standup.LinearClient, "from_env", classmethod(lambda cls: FakeLinear(linear_data())))

    def no_telegram(*args, **kwargs):
        raise AssertionError("sent without --send")

    monkeypatch.setattr(telegram_adapter, "send_html", no_telegram)

    assert standup.main([]) == 0
    assert "<b>Standup · " in capsys.readouterr().out
