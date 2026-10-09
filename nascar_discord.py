#!/usr/bin/env python3
"""Post NASCAR Cup qualifying, official race results, and upcoming reminders to Discord.

Uses publicly available NASCAR JSON feeds; no third-party dependencies required.
"""
import json
import logging
import os
import sys
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from zoneinfo import ZoneInfo

BASE = "https://cf.nascar.com/cacher"
STATE_FILE = Path(__file__).resolve().parent / "bot_state.json"
SA_TIME = ZoneInfo("Africa/Johannesburg")
UTC = timezone.utc
LOG = logging.getLogger("nascar-discord")


def get_json(url):
    req = Request(url, headers={"User-Agent": "NASCARDiscordNotifier/1.0 (personal webhook)", "Accept": "application/json"})
    with urlopen(req, timeout=25) as response:
        return json.load(response)


def parse_utc(s):
    """NASCAR event 'start_time_utc' strings omit the trailing Z in some feeds."""
    if not s or str(s).startswith("0001-"):
        return None
    try:
        dt = datetime.fromisoformat(str(s).replace("Z", "+00:00"))
        return dt.replace(tzinfo=UTC) if dt.tzinfo is None else dt.astimezone(UTC)
    except (TypeError, ValueError):
        return None


def event_time(race, run_type):
    events = [e for e in race.get("schedule", []) if e.get("run_type") == run_type]
    if run_type == 2:
        events = [e for e in events if "qual" in e.get("event_name", "").lower()] or events
    times = [parse_utc(e.get("start_time_utc")) for e in events]
    times = [t for t in times if t is not None]
    return min(times) if times else None


def load_schedule(now):
    all_races = []
    had_response = False
    for year in (now.year, now.year + 1):
        for url in (f"{BASE}/{year}/race_list_basic.json", f"{BASE}/{year}/1/race_list_basic.json"):
            try:
                data = get_json(url)
                if isinstance(data, list):
                    races = data
                elif isinstance(data, dict) and "series_1" in data:
                    races = data["series_1"]
                else:
                    raise ValueError("No Cup Series schedule found in feed")
                races = [r for r in races if r.get("series_id") == 1]
                all_races.extend(races)
                had_response = True
                LOG.info("Loaded %d NASCAR Cup events for %d", len(races), year)
                break
            except (HTTPError, URLError, TimeoutError, ValueError, OSError) as e:
                LOG.warning("Schedule URL unavailable (%s): %s", url, e)
    if not had_response:
        raise RuntimeError("Unable to retrieve NASCAR Cup schedule; no messages sent")
    return sorted(all_races, key=lambda r: (event_time(r, 3) or datetime.max.replace(tzinfo=UTC)))


def race_feed(race):
    url = f'{BASE}/{race["race_season"]}/1/{race["race_id"]}/weekend-feed.json'
    return get_json(url)


def name_of(row):
    return row.get("driver_fullname") or row.get("driver_name") or row.get("full_name") or "Unknown driver"


def qualifying_results(race, feed):
    """Use starting-grid qualifiers from the official race record when available."""
    master = (feed.get("weekend_race") or [{}])[0]
    rows = [r for r in master.get("results", []) if int(r.get("qualifying_position") or 0) > 0]
    if len(rows) >= 10:
        return sorted(rows, key=lambda r: int(r["qualifying_position"])), "qualifying_position"
    runs = [r for r in feed.get("weekend_runs", []) if r.get("run_type") == 2 and len(r.get("results") or []) >= 10]
    if runs:
        run = max(runs, key=lambda r: r.get("run_date_utc") or "")
        rows = [r for r in run["results"] if int(r.get("finishing_position") or 0) > 0]
        return sorted(rows, key=lambda r: int(r["finishing_position"])), "finishing_position"
    return [], "qualifying_position"


def finished_results(feed):
    master = (feed.get("weekend_race") or [{}])[0]
    if not master.get("inspection_complete") or int(master.get("winner_driver_id") or 0) <= 0:
        return []
    rows = [r for r in master.get("results", []) if int(r.get("finishing_position") or 0) > 0]
    return sorted(rows, key=lambda r: int(r["finishing_position"])) if len(rows) >= 10 else []


def car_line(row, pos, extra=""):
    car = row.get("car_number") or row.get("vehicle_number") or "?"
    driver = name_of(row).replace("`", "'")
    return f"**{pos:>2}.** {driver} (#{car}){extra}"


def result_embed(race, rows, kind, position_key="finishing_position"):
    race_name = race.get("race_name", "NASCAR Cup race")
    track = race.get("track_name", "Unknown track")
    if kind == "qualifying":
        title, color = f"🏁 Qualifying Results — {race_name}", 0x2D9CDB
        lines = []
        for r in rows[:45]:
            pos = int(r[position_key])
            speed = r.get("qualifying_speed") or r.get("best_lap_speed")
            info = f" — {float(speed):.3f} mph" if speed and float(speed) > 0 else ""
            lines.append(car_line(r, pos, info))
        caption = "Qualifying order; NASCAR may revise the starting grid after penalties."
    else:
        title, color = f"🏆 Official Race Results — {race_name}", 0xF5A623
        lines = [car_line(r, int(r["finishing_position"])) for r in rows[:45]]
        caption = "Posted after NASCAR reports inspection complete."
    description = f"**{track}**\n\n" + "\n".join(lines) + f"\n\n*{caption}*"
    # Discord embed descriptions have a 4096-character limit.
    description = description[:4000]
    return {"title": title[:256], "description": description, "color": color,
            "footer": {"text": "NASCAR Cup Series • Times shown in your Discord timezone"}}


def upcoming_embed(race, label, later_races=None):
    race_dt = event_time(race, 3)
    title = f"📅 {label} — {race.get('race_name', 'Next NASCAR Cup race')}"
    sa = race_dt.astimezone(SA_TIME)
    description = (f"**Track:** {race.get('track_name', 'TBA')}\n"
                   f"**Race start:** <t:{int(race_dt.timestamp())}:F> (<t:{int(race_dt.timestamp())}:R>)\n"
                   f"**South Africa (SAST):** {sa.strftime('%A %d %B %Y, %H:%M')}\n")
    if race.get("television_broadcaster"):
        description += f"**US broadcaster:** {race['television_broadcaster']}\n"
    if later_races:
        description += "\n**Coming up next:**\n"
        for later in later_races:
            when = event_time(later, 3)
            if when:
                description += (f"• {later.get('race_name', 'NASCAR Cup race')} — "
                                f"<t:{int(when.timestamp())}:F>\n")
    return {"title": title[:256], "description": description, "color": 0x46A758,
            "url": f"https://www.nascar.com/nascar-cup-series/{race['race_season']}/schedule/"}


def send(webhook_url, embed):
    payload = json.dumps({"username": "NASCAR Cup Updates", "embeds": [embed], "allowed_mentions": {"parse": []}}).encode("utf-8")
    for attempt in range(3):
        req = Request(webhook_url, data=payload,
                      headers={"Content-Type": "application/json", "User-Agent": "NASCARDiscordNotifier/1.0"},
                      method="POST")
        try:
            with urlopen(req, timeout=25) as response:
                LOG.info("Discord posted: %s (HTTP %s)", embed["title"], response.status)
                return
        except HTTPError as e:
            if e.code == 429 and attempt < 2:
                try:
                    delay = float(json.loads(e.read().decode("utf-8")).get("retry_after", 2))
                except (ValueError, KeyError):
                    delay = 2
                time.sleep(min(delay, 10))
                continue
            raise
    raise RuntimeError("Discord webhook request failed after retries")


def main():
    logging.basicConfig(level=logging.INFO, format="%(levelname)s: %(message)s")
    webhook = os.getenv("DISCORD_WEBHOOK_URL", "").strip()
    if not webhook.startswith("https://discord.com/api/webhooks/"):
        raise RuntimeError("DISCORD_WEBHOOK_URL must be set to your Discord webhook URL")

    if os.getenv("TEST_MESSAGE", "false").lower() == "true":
        send(webhook, {"title": "✅ NASCAR notifier connected!", "color": 0x46A758,
                       "description": "Test successful. Upcoming races, qualifying and official results will be posted here automatically."})
        return

    now = datetime.now(UTC)
    state = json.loads(STATE_FILE.read_text()) if STATE_FILE.exists() else {"sent": []}
    sent = set(state.get("sent", []))

    def record(key):
        sent.add(key)
        STATE_FILE.write_text(json.dumps({"sent": sorted(sent)}, indent=2) + "\n")

    races = load_schedule(now)
    upcoming = [r for r in races if (t := event_time(r, 3)) and t > now]
    upcoming.sort(key=lambda r: event_time(r, 3))
    next_race = upcoming[0] if upcoming else None

    if next_race:
        start = event_time(next_race, 3)
        key_prefix = f'{next_race["race_season"]}:{next_race["race_id"]}'
        hours = (start - now).total_seconds() / 3600
        sa_now = now.astimezone(SA_TIME)
        weekly_key = "weekly:" + key_prefix
        one_day_key = "24h:" + key_prefix
        if sa_now.weekday() == 4 and sa_now.hour >= 18 and 0 < hours <= 8 * 24 and weekly_key not in sent:
            send(webhook, upcoming_embed(next_race, "Upcoming races", upcoming[1:3]))
            record(weekly_key)
        if 0 < hours <= 24 and one_day_key not in sent:
            send(webhook, upcoming_embed(next_race, "Race starts within 24 hours"))
            record(one_day_key)

    # Only inspect this week's events; don't replay the entire season on first run.
    for race in races:
        start = event_time(race, 3)
        if start is None or not start - timedelta(days=3) <= now <= start + timedelta(days=4):
            continue
        qtime = event_time(race, 2)
        key_prefix = f'{race["race_season"]}:{race["race_id"]}'
        qual_key, result_key = "qual:" + key_prefix, "race:" + key_prefix
        wants_qual = (qtime is not None and qtime + timedelta(minutes=75) <= now
                      and now <= start + timedelta(days=1) and qual_key not in sent)
        wants_result = (start + timedelta(hours=2) <= now and result_key not in sent)
        if not (wants_qual or wants_result):
            continue
        try:
            feed = race_feed(race)
        except (HTTPError, URLError, TimeoutError, ValueError, OSError) as e:
            LOG.warning("No weekend feed for %s: %s", race.get("race_name"), e)
            continue
        if wants_qual:
            master = (feed.get("weekend_race") or [{}])[0]
            if int(master.get("pole_winner_driver_id") or 0) > 0:
                rows, position_key = qualifying_results(race, feed)
                if len(rows) >= 10:
                    send(webhook, result_embed(race, rows, "qualifying", position_key))
                    record(qual_key)
        if wants_result:
            rows = finished_results(feed)
            if rows:
                send(webhook, result_embed(race, rows, "race"))
                record(result_key)

    LOG.info("Check complete (%d unique notifications recorded)", len(sent))


if __name__ == "__main__":
    try:
        main()
    except Exception:
        LOG.exception("NASCAR notifier failed")
        sys.exit(1)
