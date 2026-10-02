"""The daily standup: cycle progress, what's done, in progress, stuck, blocked and waiting on you.

    Linear (read-only) + the work-request store ─► Standup ─► render() ─► Telegram HTML

No model is involved, on purpose. A standup is a query plus formatting, so it
costs no tokens, and the same data always renders the same message, which is
what the tests check.

Nothing is cut: every item is listed with its full title. A long standup
becomes several Telegram messages, split between lines (send_html), never
mid-item.

"Done" and "in progress" are team-wide, not just the active cycle: adhoc work
often has no cycle and is still the day's work. Cycle numbers (progress,
points, days left) come from the active cycle.

Time is the point. In progress for more than STUCK_DAYS isn't really in
progress, so it's listed as stuck, oldest first. Anything waiting on you
says for how long, and the top of the message names what's stuck or has
waited longest, so it's the first thing read.

"Waiting on you" is everything that can't move without a person:
- Linear issues with a gate:awaiting-approval or gate:needs-human label,
  aged from when the label was added;
- Cycle Runner commits that are ready for review and not yet approved or
  rejected (V1.3+), aged from when the run finished.

Run it:  op run --env-file .env -- uv run python -m cycle_runner.standup [--send]
Without --send it prints the message instead of sending it.
"""

import argparse
import asyncio
import html
import logging
import os
import re
import sys
from collections.abc import Awaitable, Callable, Iterable
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

from cycle_runner.linear_client import LinearClient, LinearError
from cycle_runner.work_requests import WorkRequestStore, db_path_from_env

log = logging.getLogger(__name__)

TIMEZONE = ZoneInfo("America/New_York")
GATE_LABELS = ("gate:awaiting-approval", "gate:needs-human")
CLOSED_STATE_TYPES = {"completed", "canceled"}
STUCK_DAYS = 7  # in progress for longer than a cycle: say so, don't call it progress
WAITING_ALERT_DAYS = 2  # waiting on you longer than this is called out at the top
PROGRESS_BAR = 10
# File names Telegram would otherwise turn into links (.md and .py are country domains).
FILE_NAME = re.compile(r"(?<![\w/])([\w./-]*\w\.(?:md|py|sh|yaml|yml|toml|json|txt|html|ts|js))\b")

STANDUP = """
query ($team: String!, $since: DateTimeOrDuration!) {
  teams(first: 1, filter: {key: {eq: $team}}) {
    nodes {
      activeCycle {
        number startsAt endsAt progress
        issues(first: 250) { nodes { identifier estimate state { type } } }
      }
    }
  }
  done: issues(first: 100, filter: {team: {key: {eq: $team}}, completedAt: {gt: $since}}) {
    nodes { identifier title url estimate completedAt }
  }
  started: issues(first: 100, filter: {team: {key: {eq: $team}}, state: {type: {eq: "started"}}}) {
    nodes {
      identifier title url estimate startedAt
      inverseRelations { nodes { type issue { identifier state { type } } } }
    }
  }
  gated: issues(first: 50, filter: {
    team: {key: {eq: $team}}, state: {type: {nin: ["completed", "canceled"]}},
    labels: {name: {in: ["gate:awaiting-approval", "gate:needs-human"]}}
  }) {
    nodes {
      identifier title url estimate createdAt
      labels { nodes { name } }
      history(first: 50) { nodes { createdAt addedLabels { name } } }
    }
  }
}
"""


@dataclass(frozen=True)
class Item:
    """One line of the standup: an issue or a work request."""

    id: str
    title: str
    url: str | None = None
    points: float | None = None
    days: int | None = None  # in progress, or waiting, for this many days
    note: str = ""  # e.g. "blocked by SB-12", "needs you"


@dataclass(frozen=True)
class CycleProgress:
    number: int
    day: int  # 1-based day of the cycle, today included
    days: int
    days_left: int
    percent: int
    points_done: float
    points_total: float


@dataclass(frozen=True)
class Standup:
    today: date
    since: date
    cycle: CycleProgress | None
    done: list[Item] = field(default_factory=list)  # newest first
    in_progress: list[Item] = field(default_factory=list)  # newest first
    stuck: list[Item] = field(default_factory=list)  # in progress > STUCK_DAYS, oldest first
    blocked: list[Item] = field(default_factory=list)
    waiting_on_you: list[Item] = field(default_factory=list)  # longest wait first


# --- when ----------------------------------------------------------------------


def previous_weekday(today: date) -> date:
    """The last working day before today: Friday, when today is Monday (or the weekend)."""
    day = today - timedelta(days=1)
    while day.weekday() >= 5:
        day -= timedelta(days=1)
    return day


def start_of(day: date) -> datetime:
    return datetime.combine(day, time.min, tzinfo=TIMEZONE)


def _local_date(timestamp: str) -> date:
    return datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(TIMEZONE).date()


# --- collect -------------------------------------------------------------------


async def collect(
    query: Callable[[str, dict], Awaitable[dict]],
    *,
    team: str,
    now: datetime,
    store: WorkRequestStore | None = None,
) -> Standup:
    """Everything the standup says, read once. Linear errors propagate: a standup with holes would mislead."""
    today = now.astimezone(TIMEZONE).date()
    since = previous_weekday(today)
    data = await query(STANDUP, {"team": team, "since": start_of(since).isoformat()})

    teams = data["teams"]["nodes"]
    cycle = _cycle_progress(teams[0]["activeCycle"], today) if teams and teams[0]["activeCycle"] else None

    done = [_item(issue) for issue in sorted(data["done"]["nodes"], key=lambda i: i["completedAt"], reverse=True)]

    in_progress, stuck, blocked = [], [], []
    for issue in sorted(data["started"]["nodes"], key=lambda i: i.get("startedAt") or "", reverse=True):
        days = (today - _local_date(issue["startedAt"])).days if issue.get("startedAt") else None
        blockers = _open_blockers(issue)
        if blockers:
            blocked.append(_item(issue, days=days, note=f"blocked by {', '.join(blockers)}"))
        elif days is not None and days > STUCK_DAYS:
            stuck.append(_item(issue, days=days))
        else:
            in_progress.append(_item(issue, days=days))
    stuck.reverse()  # oldest first: the worst is the first read

    waiting = [_item(issue, days=_waiting_days(issue, today), note=_gate_note(issue)) for issue in data["gated"]["nodes"]]
    if store is not None:
        waiting += _work_requests_waiting(store, today)
        blocked += _work_requests_failed(store, start_of(since))
    waiting.sort(key=lambda i: (-(i.days or 0), i.id))

    return Standup(today=today, since=since, cycle=cycle, done=done, in_progress=in_progress, stuck=stuck,
                   blocked=blocked, waiting_on_you=waiting)


def _cycle_progress(cycle: dict, today: date) -> CycleProgress:
    start, end = _local_date(cycle["startsAt"]), _local_date(cycle["endsAt"])
    issues = [i for i in cycle["issues"]["nodes"] if i["state"]["type"] != "canceled"]
    return CycleProgress(
        number=int(cycle["number"]),
        day=min(max((today - start).days + 1, 1), (end - start).days),
        days=(end - start).days,
        days_left=max((end - today).days, 0),
        percent=round(cycle["progress"] * 100),
        points_done=sum(i["estimate"] or 0 for i in issues if i["state"]["type"] == "completed"),
        points_total=sum(i["estimate"] or 0 for i in issues),
    )


def _item(issue: dict, *, days: int | None = None, note: str = "") -> Item:
    return Item(id=issue["identifier"], title=issue["title"], url=issue.get("url"), points=issue.get("estimate"),
                days=days, note=note)


def _open_blockers(issue: dict) -> list[str]:
    return [
        relation["issue"]["identifier"]
        for relation in issue.get("inverseRelations", {}).get("nodes", [])
        if relation["type"] == "blocks" and relation["issue"]["state"]["type"] not in CLOSED_STATE_TYPES
    ]


def _gate_note(issue: dict) -> str:
    labels = {label["name"] for label in issue["labels"]["nodes"]}
    return "needs you" if "gate:needs-human" in labels else "awaiting your approval"


def _waiting_days(issue: dict, today: date) -> int | None:
    """Days since the gate label it carries now was added (the latest such event)."""
    current = {label["name"] for label in issue["labels"]["nodes"]} & set(GATE_LABELS)
    added = [
        event["createdAt"]
        for event in (issue.get("history") or {}).get("nodes", [])
        if current & {label["name"] for label in event.get("addedLabels") or []}
    ]
    when = max(added) if added else issue.get("createdAt")
    return (today - _local_date(when)).days if when else None


def _work_requests_waiting(store: WorkRequestStore, today: date) -> list[Item]:
    """Commits Cycle Runner made that nobody has approved or rejected yet."""
    return [
        Item(id=wr.work_request_id, title=f"{wr.issue_id} {wr.title_at_approval}",
             days=(today - wr.finished_at.astimezone(TIMEZONE).date()).days if wr.finished_at else None,
             note=f"commit {wr.commit_sha[:7]} to review")
        for wr in store.list_all()
        if wr.status == "completed" and wr.outcome == "changed" and wr.commit_sha
        and not store.approvals_for(wr.work_request_id)
    ]


def _work_requests_failed(store: WorkRequestStore, since: datetime) -> list[Item]:
    return [
        Item(id=wr.work_request_id, title=f"{wr.issue_id} {wr.title_at_approval}", note="run failed")
        for wr in store.list_all()
        if wr.status == "failed" and wr.finished_at and wr.finished_at >= since
    ]


# --- render --------------------------------------------------------------------


def render(standup: Standup) -> str:
    """The standup as Telegram HTML. Every item in full, every id a link; lines are self-contained."""
    lines = [f"☀️ <b>Standup · {standup.today:%a %-d %b}</b>"]
    if standup.cycle:
        c = standup.cycle
        filled = round(c.percent / 100 * PROGRESS_BAR)
        lines += [
            f"<b>Cycle {c.number}</b> · day {c.day} of {c.days} · {_plural(c.days_left, 'day')} left",
            f"<code>{'▓' * filled}{'░' * (PROGRESS_BAR - filled)}</code> {c.percent}% · "
            f"{_points(c.points_done)} of {_pts(c.points_total)}",
        ]
    else:
        lines.append("No active cycle.")

    alerts = _alerts(standup)
    if alerts:
        lines += ["", "🚨 <b>Needs attention</b>", *alerts]

    since = "yesterday" if standup.since == standup.today - timedelta(days=1) else f"{standup.since:%A}"
    done_points = sum(i.points or 0 for i in standup.done)
    lines += ["", f"✅ <b>Done since {since}</b> · {_plural(len(standup.done), 'issue')} · {_pts(done_points)}"]
    lines += [_line(i, _points_tag(i)) for i in standup.done] or ["<i>Nothing closed.</i>"]

    lines += ["", f"🔨 <b>In progress</b> · {len(standup.in_progress)}"]
    lines += [_line(i, _age(i)) for i in standup.in_progress] or ["<i>Nothing in progress.</i>"]

    if standup.stuck:
        lines += ["", f"🐢 <b>Stuck in progress</b> (&gt;{STUCK_DAYS} days) · {len(standup.stuck)}"]
        lines += [_line(i, _age(i, strong=True)) for i in standup.stuck]

    if standup.blocked:
        lines += ["", f"⛔ <b>Blocked</b> · {len(standup.blocked)}"]
        lines += [_line(i, " · ".join(filter(None, [_text(i.note), _age(i)]))) for i in standup.blocked]

    if standup.waiting_on_you:
        lines += ["", f"⏳ <b>Waiting on you</b> · {len(standup.waiting_on_you)}"]
        lines += [_line(i, " · ".join(filter(None, [_text(i.note), _waited(i)]))) for i in standup.waiting_on_you]
    return "\n".join(lines)


def _alerts(standup: Standup) -> list[str]:
    alerts = []
    if standup.stuck:
        oldest = standup.stuck[0]
        alerts.append(f"• {_plural(len(standup.stuck), 'issue')} stuck in progress; "
                      f"oldest {_link(oldest)}, <b>{oldest.days} days</b>")
    overdue = [i for i in standup.waiting_on_you if (i.days or 0) > WAITING_ALERT_DAYS]
    if overdue:
        alerts.append(f"• {_plural(len(overdue), 'item')} waiting on you over {WAITING_ALERT_DAYS} days; "
                      f"longest {_link(overdue[0])}, <b>{overdue[0].days} days</b>")
    if standup.blocked:
        alerts.append(f"• {len(standup.blocked)} blocked")
    return alerts


def _line(item: Item, tail: str) -> str:
    return f"• {_link(item)} {_title(item.title)}" + (f" · {tail}" if tail else "")


def _points_tag(item: Item) -> str:
    return f"<b>{_pts(item.points)}</b>" if item.points else "<i>no estimate</i>"


def _age(item: Item, *, strong: bool = False) -> str:
    if item.days is None:
        return ""
    text = "started today" if item.days == 0 else f"{_plural(item.days, 'day')}"
    return f"<b>{text}</b>" if strong else text


def _waited(item: Item) -> str:
    if item.days is None:
        return ""
    text = "since today" if item.days == 0 else f"waiting {_plural(item.days, 'day')}"
    return f"🔴 <b>{text}</b>" if item.days > WAITING_ALERT_DAYS else text


def _text(value: str) -> str:
    return html.escape(value, quote=False)  # Telegram needs only <, > and & escaped in text


def _title(title: str) -> str:
    return FILE_NAME.sub(r"<code>\1</code>", _text(title))


def _link(item: Item) -> str:
    text = _text(item.id)
    return f'<a href="{html.escape(item.url)}">{text}</a>' if item.url else f"<b>{text}</b>"


def _points(value: float | None) -> str:
    return f"{value or 0:g}"


def _pts(value: float | None) -> str:
    return f"{_points(value)} pt" if value == 1 else f"{_points(value)} pts"


def _plural(n: int, word: str) -> str:
    return f"{n} {word}" if n == 1 else f"{n} {word}s"


# --- command line ----------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m cycle_runner.standup", description=__doc__.splitlines()[0])
    parser.add_argument("--send", action="store_true", help="send to Telegram (default: print)")
    args = parser.parse_args(list(argv) if argv is not None else None)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)  # Telegram API URLs contain the bot token

    path = db_path_from_env()
    store = WorkRequestStore(path) if os.path.exists(path) else None
    try:
        standup = asyncio.run(collect(LinearClient.from_env().query, team=os.environ.get("LINEAR_TEAM_KEY", "SB"),
                                      now=datetime.now(TIMEZONE), store=store))
    except LinearError as exc:
        print(f"standup: Linear is unavailable: {exc}", file=sys.stderr)
        return 1
    message = render(standup)
    if not args.send:
        print(message)
        return 0

    from cycle_runner.telegram_adapter import TelegramConfig, send_html

    config = TelegramConfig.from_env()
    asyncio.run(send_html(config.bot_token, sorted(config.allowed_user_ids), message))
    log.info("standup sent to %d chat(s)", len(config.allowed_user_ids))
    return 0


if __name__ == "__main__":
    sys.exit(main())
