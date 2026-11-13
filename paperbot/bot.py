#!/usr/bin/env python3
"""Paper trading bot: runs the day-trader skills on a pretend account.

Run once an hour while the NYSE is open (GitHub Actions does this). Each run:

  1. Manages open positions: replays the 5-minute bars since the last run and
     closes a position at its stop, its target, or the flat-by bar (15:55 ET).
  2. Looks for new trades (09:50 to 15:30 ET): trader-scanner (opening-range
     breakouts on the universe) -> trader-entry -> trader-exit (stop + 2R target
     from the CURRENT price) -> trader-timemachine (skip if the replays expect a
     loss) -> trader-budget (size) -> trader-risk (PASS or skip). A PASS is
     filled on paper at the current price. Nothing is ever sent to a broker.
  3. After the close: writes the day's report to paperbot/reports/ and pushes it
     to the ntfy app (NTFY_TOPIC) and/or emails it (SMTP_USER, SMTP_PASSWORD, MAIL_TO).

The account lives in paperbot/ledger.json. Everything is fake money.

Usage:
  python3 paperbot/bot.py                       # one run, real clock
  python3 paperbot/bot.py --now "2026-11-12 11:05"   # replay a past moment
  python3 paperbot/bot.py --report-only        # (re)build today's report
  python3 paperbot/bot.py --test-notify        # one test push to the ntfy app
"""
import argparse
import datetime as dt
import importlib.util
import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.error
import urllib.request
from email.message import EmailMessage
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SKILLS = ROOT / ".claude" / "skills"
BOT = ROOT / "paperbot"
WORK = BOT / "work"                      # the skills' cwd: they read/write WORK/trader/
TRADER = WORK / "trader"
LEDGER = BOT / "ledger.json"
REPORTS = BOT / "reports"
CONFIG = BOT / "config.json"
UNIVERSE = BOT / "universe.txt"
UA = "Mozilla/5.0 (compatible; paperbot/1.0)"
PY = sys.executable

_spec = importlib.util.spec_from_file_location("alerts", SKILLS / "trader-alerts" / "alerts.py")
alerts = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(alerts)


def log(*a):
    print(*a, flush=True)


# ---------------------------------------------------------------- files

def read_json(path, default=None):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        return default


def write_json(path, doc):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(doc, indent=2) + "\n")
    tmp.replace(path)


def load_ledger(cfg):
    led = read_json(LEDGER)
    if not led:
        led = {"started": None, "starting_cash": cfg["budget"], "cash": cfg["budget"],
               "positions": [], "trades": [], "days": {}, "seen_triggers": {}}
    return led


# ---------------------------------------------------------------- skills

def skill(script, *args, now=None, ok=(0,)):
    """Run a skill script in WORK with --json; returns (exit code, parsed JSON or None, stderr)."""
    cmd = [PY, str(SKILLS / script), *args, "--json"]
    if now:
        cmd += ["--now", now]
    r = subprocess.run(cmd, cwd=WORK, capture_output=True, text=True, timeout=600)
    try:
        doc = json.loads(r.stdout) if r.stdout.strip() else None
    except ValueError:
        doc = None
    return r.returncode, doc, (r.stderr or r.stdout)[-600:]


# ---------------------------------------------------------------- bars

def fetch_bars(sym, day):
    """5-minute bars for one New York date: [{t: 'HH:MM', o, h, l, c}] in time order."""
    url = "https://query2.finance.yahoo.com/v8/finance/chart/%s?range=5d&interval=5m" % sym
    for attempt in range(3):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA})
            with urllib.request.urlopen(req, timeout=25) as r:
                data = json.loads(r.read().decode())
            break
        except (urllib.error.URLError, ValueError, OSError) as e:
            if attempt == 2:
                raise RuntimeError("bars for %s: %s" % (sym, e))
            time.sleep(5 * (attempt + 1))
    res = data["chart"]["result"][0]
    q = res["indicators"]["quote"][0]
    out = []
    for i, ts in enumerate(res.get("timestamp") or []):
        local = alerts.to_eastern(dt.datetime.fromtimestamp(ts, dt.timezone.utc).replace(tzinfo=None))
        if local.date() != day:
            continue
        o, h, l, c = q["open"][i], q["high"][i], q["low"][i], q["close"][i]
        if None in (o, h, l, c):
            continue
        out.append({"t": local.strftime("%H:%M"), "o": o, "h": h, "l": l, "c": c})
    return out


def closed_bars(bars, now_local):
    """Bars whose 5 minutes are over at now_local."""
    cut = (now_local - dt.timedelta(minutes=5)).strftime("%H:%M")
    return [b for b in bars if b["t"] <= cut]


# ---------------------------------------------------------------- positions

def manage_positions(led, cfg, today, now_local, status):
    flat_by = cfg.get("flat_by", "15:55")
    after_close = not status["open"] and status.get("reason") == "after the close"
    for pos in list(led["positions"]):
        try:
            bars = fetch_bars(pos["symbol"], dt.date.fromisoformat(pos["date"]))
        except Exception as e:  # keep the position, try next run
            log("  could not read bars for %s: %s" % (pos["symbol"], e))
            continue
        if pos["date"] != today.isoformat():
            bars_done = bars                        # an older day: the whole session is over
        else:
            bars_done = bars if after_close else closed_bars(bars, now_local)
        exit_px = reason = exit_t = None
        for b in bars_done:
            if b["t"] < pos["checked_through"]:
                continue
            if b["t"] >= flat_by:
                exit_px, reason, exit_t = b["o"], "flat by %s" % flat_by, b["t"]
                break
            # a gap through a level fills at the open; a bar touching both counts as the stop
            if b["l"] <= pos["stop"]:
                exit_px, reason, exit_t = min(b["o"], pos["stop"]), "stop", b["t"]
                break
            if b["h"] >= pos["target"]:
                exit_px, reason, exit_t = max(b["o"], pos["target"]), "target", b["t"]
                break
        if exit_px is None and bars_done:
            pos["checked_through"] = (dt.datetime.strptime(bars_done[-1]["t"], "%H:%M")
                                      + dt.timedelta(minutes=5)).strftime("%H:%M")
            if (after_close or pos["date"] != today.isoformat()) and bars:
                exit_px, reason, exit_t = bars[-1]["c"], "session ended", bars[-1]["t"]
        if exit_px is None:
            log("  holding %s %d @ %.2f (stop %.2f, target %.2f)"
                % (pos["symbol"], pos["qty"], pos["entry"], pos["stop"], pos["target"]))
            continue
        close_position(led, pos, round(exit_px, 2), reason, exit_t)


def close_position(led, pos, px, reason, t):
    pnl = round((px - pos["entry"]) * pos["qty"], 2)
    r_mult = round((px - pos["entry"]) / pos["risk_per_share"], 2) if pos["risk_per_share"] else None
    led["cash"] = round(led["cash"] + px * pos["qty"], 2)
    led["positions"].remove(pos)
    trade = dict(pos, exit=px, exit_time=t, exit_reason=reason, pnl=pnl, R=r_mult)
    trade.pop("checked_through", None)
    led["trades"].append(trade)
    log("  CLOSED %s %d: %.2f -> %.2f (%s) P&L %+.2f, %sR"
        % (pos["symbol"], pos["qty"], pos["entry"], px, reason, pnl, r_mult))
    write_broker_state(led)


def write_broker_state(led):
    """What trader-risk reads: today's placed entries and the cash we have."""
    orders = []
    for t in led["trades"] + led["positions"]:
        orders.append({"date": t["date"], "symbol": t["symbol"], "side": "buy", "kind": "entry",
                       "status": "placed", "max_loss": t["max_loss"], "qty": t["qty"]})
        if "exit" in t:
            orders.append({"date": t["date"], "symbol": t["symbol"], "side": "sell", "kind": "exit",
                           "status": "filled", "qty": t["qty"]})
    write_json(TRADER / "state" / "broker.json", {"orders": orders})
    write_json(TRADER / "portfolio.json", {
        "cash": led["cash"],
        "positions": [{"ticker": p["symbol"], "shares": p["qty"],
                       "lots": [{"shares": p["qty"], "cost": round(p["qty"] * p["entry"], 2)}]}
                      for p in led["positions"]]})


# ---------------------------------------------------------------- entries

def look_for_entries(led, cfg, today, now_local, now_arg):
    hhmm = now_local.strftime("%H:%M")
    if not (cfg["no_entries_before"] <= hhmm < cfg["no_entries_after"]):
        log("  no new entries at %s (window %s-%s)" % (hhmm, cfg["no_entries_before"], cfg["no_entries_after"]))
        return
    traded_today = [t for t in led["trades"] + led["positions"] if t["date"] == today.isoformat()]
    if len(traded_today) >= cfg["max_trades_per_day"]:
        log("  %d trades today: the daily maximum" % len(traded_today))
        return
    held = {p["symbol"] for p in led["positions"]}
    done = {t["symbol"] for t in traded_today}
    code, scan, err = skill("trader-scanner/scan.py", "orb", "--watchlist", str(UNIVERSE),
                            "--config", "trader/config.json", "--quiet", now=now_arg)
    if not scan:
        log("  scanner failed: %s" % err.strip())
        return
    hits = [h for h in scan.get("hits", []) if h["symbol"] not in held | done]
    # freshest breakouts first: a trigger an hour old has usually run already
    hits.sort(key=lambda h: (-h["bar_index"], h["pct_over"]))
    log("  scanner: %d opening-range breakouts, %d new to us" % (len(scan.get("hits", [])), len(hits)))
    seen = led["seen_triggers"].setdefault(today.isoformat(), [])
    for h in hits[: cfg.get("max_candidates", 6)]:
        sym = h["symbol"]
        key = "%s@%s" % (sym, h["at"])
        if key in seen:
            continue
        seen.append(key)
        if try_trade(led, cfg, sym, today, now_local, now_arg):
            traded_today.append(sym)
            if len(traded_today) >= cfg["max_trades_per_day"]:
                return


def try_trade(led, cfg, sym, today, now_local, now_arg):
    code, entry, err = skill("trader-entry/entry.py", "check", sym, "--write", now=now_arg)
    if not entry or entry.get("status") != "triggered":
        log("  %s: entry says %s" % (sym, (entry or {}).get("status", err.strip()[:120])))
        return False
    try:
        bars = closed_bars(fetch_bars(sym, today), now_local)
    except Exception as e:
        log("  %s: %s" % (sym, e))
        return False
    if not bars:
        return False
    price = round(bars[-1]["c"], 2)
    # plan from the price we would actually pay now, not the old trigger price
    code, plan, err = skill("trader-exit/exit.py", "plan", "--entry", str(price), "--write", now=now_arg)
    if not plan or code != 0:
        log("  %s: no valid exit plan at %.2f: %s" % (sym, price, err.strip()[:160]))
        return False
    plan_doc = read_json(TRADER / "plan.json") or {}
    plan_doc.update({"entry_time": bars[-1]["t"], "entry_bar": len(bars) - 1})
    plan_doc["entry_time"] = (dt.datetime.strptime(bars[-1]["t"], "%H:%M") + dt.timedelta(minutes=5)).strftime("%H:%M")
    write_json(TRADER / "plan.json", plan_doc)
    code, tm, err = skill("trader-timemachine/timemachine.py", "run", "--replays", str(cfg.get("replays", 3000)),
                          now=now_arg)
    tm_r = hist_r = None
    if tm:
        tm_r = tm["mc"]["avg_R"]
        hist_r = (tm.get("history") or {}).get("avg_R")
        if tm_r < cfg.get("min_timemachine_avg_r", 0.0):
            log("  %s: time machine expects %.3fR on average: skipped" % (sym, tm_r))
            return False
    else:
        log("  %s: time machine could not run (%s): skipped" % (sym, err.strip()[:160]))
        return False
    write_json(TRADER / "prices.json", {
        "as_of": dt.datetime.now(dt.timezone.utc).replace(tzinfo=None).strftime("%Y-%m-%dT%H:%M:%SZ") if not now_arg else
        alerts.parse_now(now_arg).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "source": "yahoo", "prices": {sym: price}})
    code, sized, err = skill("trader-budget/size.py", "--write", now=now_arg)
    s = (sized or {}).get("sizing") or {}
    if not sized or s.get("refusal") or not s.get("qty"):
        log("  %s: budget refused: %s" % (sym, s.get("refusal") or err.strip()[:160]))
        return False
    code, risk, err = skill("trader-risk/check.py", "--no-write", now=now_arg)
    res = (risk or {}).get("results") or []
    if not res or res[0]["verdict"] != "PASS":
        log("  %s: risk BLOCK: %s" % (sym, (res[0]["reasons"] if res else err.strip()[:160])))
        return False
    qty = int(float(s["qty"]))
    cost = round(qty * price, 2)
    if cost > led["cash"]:
        log("  %s: not enough paper cash" % sym)
        return False
    pos = {"symbol": sym, "date": today.isoformat(), "qty": qty, "entry": price,
           "entry_time": plan_doc["entry_time"], "stop": plan["stop"], "target": plan["target"],
           "risk_per_share": plan["risk_per_share"], "max_loss": round(qty * plan["risk_per_share"], 2),
           "opening_range": plan.get("opening_range"), "timemachine_avg_R": tm_r,
           "history_avg_R": hist_r, "checked_through": plan_doc["entry_time"]}
    led["cash"] = round(led["cash"] - cost, 2)
    led["positions"].append(pos)
    write_broker_state(led)
    log("  BOUGHT %d %s @ %.2f (stop %.2f, target %.2f, max loss $%.2f, time machine %+.3fR)"
        % (qty, sym, price, pos["stop"], pos["target"], pos["max_loss"], tm_r))
    return True


# ---------------------------------------------------------------- report

def equity(led):
    return round(led["cash"] + sum(p["qty"] * p["entry"] for p in led["positions"]), 2)


def build_report(led, today):
    d = today.isoformat()
    todays = [t for t in led["trades"] if t["date"] == d]
    day_pnl = round(sum(t["pnl"] for t in todays), 2)
    eq = equity(led)
    start = led["starting_cash"]
    all_t = led["trades"]
    wins = [t for t in all_t if t["pnl"] > 0]
    lines = ["Paper trading report for %s" % today.strftime("%A %d %B %Y"),
             "(Fake money. Nothing was sent to a broker.)", "",
             "Account value  $%s" % format(eq, ",.2f"),
             "Today's P&L    %+.2f" % day_pnl,
             "Total P&L      %+.2f (%+.2f%% since %s, starting with $%s)"
             % (eq - start, (eq - start) / start * 100, led.get("started"), format(start, ",.0f")),
             "Cash           $%s" % format(led["cash"], ",.2f"), ""]
    if todays:
        lines.append("Today's trades (times are New York, out = the 5-minute bar that hit):")
        for t in todays:
            lines.append("  %-6s %3d sh  in %s @ %.2f  out in the %s bar @ %.2f  %-14s %+8.2f  %+.2fR"
                         % (t["symbol"], t["qty"], t["entry_time"], t["entry"], t["exit_time"], t["exit"],
                            t["exit_reason"], t["pnl"], t["R"] or 0))
    else:
        lines.append("No trades today.")
    if led["positions"]:
        lines.append("")
        lines.append("Still open (should not happen for a day trade; closed next run):")
        for p in led["positions"]:
            lines.append("  %s %d @ %.2f" % (p["symbol"], p["qty"], p["entry"]))
    lines += ["", "All time: %d trades, %d winners (%.0f%%), average %.2fR"
              % (len(all_t), len(wins), (len(wins) / len(all_t) * 100) if all_t else 0,
                 (sum(t["R"] or 0 for t in all_t) / len(all_t)) if all_t else 0)]
    syms = sorted({t["symbol"] for t in todays})
    if syms:
        code, news, err = skill("trader-news/news.py", "--tickers", *syms, "--no-sec", "--dry-run",
                                "--hours", "24", "--all")
        items = (news or {}).get("items") or []
        if items:
            lines += ["", "News on today's tickers:"]
            for it in items[:10]:
                lines.append("  %s  %s" % (",".join(it.get("tickers", [])) or it.get("ticker", ""),
                                           it.get("title", "")))
    lines += ["", "Rules: $%s budget, max $%s loss a trade, $%s a day, %d trades a day, "
              "opening-range breakouts, stop at the range midpoint, 2R target, flat by 15:55 ET."
              % tuple([format(led["starting_cash"], ",.0f")] + [str(x) for x in
                      (read_json(CONFIG)["max_loss_per_trade_usd"], read_json(CONFIG)["max_daily_loss_usd"])]
                      + [read_json(CONFIG)["max_trades_per_day"]]),
              "Education and tooling, not financial advice."]
    return "\n".join(lines) + "\n", day_pnl, eq


def send_email(subject, body):
    host = os.environ.get("SMTP_HOST") or "mail.gmx.com"
    user, pw, to = os.environ.get("SMTP_USER"), os.environ.get("SMTP_PASSWORD"), os.environ.get("MAIL_TO")
    if not (user and pw and to):
        log("  email skipped: SMTP_USER, SMTP_PASSWORD and MAIL_TO are not all set")
        return False
    msg = EmailMessage()
    msg["From"], msg["To"], msg["Subject"] = user, to, subject
    msg.set_content(body)
    with smtplib.SMTP(host, int(os.environ.get("SMTP_PORT") or "587"), timeout=30) as s:
        s.starttls(context=ssl.create_default_context())
        s.login(user, pw)
        s.send_message(msg)
    log("  report emailed")
    return True


def send_ntfy(title, body, tags="chart_with_upwards_trend"):
    """Push to the ntfy app: POST https://ntfy.sh/<NTFY_TOPIC>. The topic name is the
    only key, so it lives in a repository secret, never in this repo."""
    topic = (os.environ.get("NTFY_TOPIC") or "").strip()
    if not topic:
        log("  ntfy skipped: NTFY_TOPIC is not set")
        return False
    server = (os.environ.get("NTFY_SERVER") or "https://ntfy.sh").rstrip("/")
    data = body.encode("utf-8")
    if len(data) > 4000:                      # ntfy turns longer messages into a file attachment
        data = data[:3900].decode("utf-8", "ignore").encode("utf-8") + b"\n... (full report in paperbot/reports/)"
    req = urllib.request.Request(server + "/" + topic, data=data, method="POST",
                                 headers={"Title": title.encode("ascii", "replace").decode(),
                                          "Tags": tags, "User-Agent": UA})
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()
    log("  report pushed to ntfy")
    return True


def deliver(title, body):
    """Send the report every way that is configured; True if any worked."""
    sent = False
    for fn in (send_ntfy, send_email):
        try:
            sent = fn(title, body) or sent
        except Exception as e:
            log("  %s failed: %s" % (fn.__name__, e))
    return sent


# ---------------------------------------------------------------- main

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--now", help='pretend it is this New York time, "YYYY-MM-DD HH:MM"')
    ap.add_argument("--report-only", action="store_true")
    ap.add_argument("--no-send", "--no-email", dest="no_send", action="store_true",
                    help="build the report but do not push or email it")
    ap.add_argument("--test-notify", action="store_true", help="send one test notification and stop")
    a = ap.parse_args(argv)
    if a.test_notify:
        ok = deliver("Paper trading bot: test", "If you can read this on your phone, the daily report "
                     "will reach you here after each trading day.")
        return 0 if ok else 1

    cfg = read_json(CONFIG)
    TRADER.mkdir(parents=True, exist_ok=True)
    skill_cfg = {k: v for k, v in cfg.items() if k not in ("max_candidates", "replays", "min_timemachine_avg_r")}
    write_json(TRADER / "config.json", skill_cfg)

    now_utc = alerts.parse_now(a.now) if a.now else dt.datetime.now(dt.timezone.utc).replace(tzinfo=None)
    now_local = alerts.to_eastern(now_utc)
    today = now_local.date()
    status = alerts.market_status(now_utc)
    log("paperbot %s ET: %s" % (now_local.strftime("%Y-%m-%d %H:%M"), status["reason"] or "open"))

    led = load_ledger(cfg)
    led["started"] = led["started"] or today.isoformat()
    write_broker_state(led)

    if a.report_only:
        status = dict(status, open=False, reason="after the close")
    trading_day, _ = alerts.trading_day(today)
    if not trading_day:
        log("  not a trading day")
        return 0

    if led["positions"]:
        manage_positions(led, cfg, today, now_local, status)
    if status["open"] and not a.report_only:
        look_for_entries(led, cfg, today, now_local, a.now)

    day = led["days"].setdefault(today.isoformat(), {})
    if status.get("reason") == "after the close" and not led["positions"] and (a.report_only or not day.get("reported")):
        body, day_pnl, eq = build_report(led, today)
        REPORTS.mkdir(parents=True, exist_ok=True)
        (REPORTS / ("%s.txt" % today.isoformat())).write_text(body)
        day.update({"pnl": day_pnl, "equity": eq})
        log(body)
        subject = "Paper trading %s: %+.2f today, account $%s" % (today.isoformat(), day_pnl, format(eq, ",.2f"))
        sent = False if a.no_send else deliver(subject, body)
        day["reported"] = True
        day["sent"] = sent
    # keep the trigger memory short
    led["seen_triggers"] = {k: v for k, v in led["seen_triggers"].items() if k >= today.isoformat()}
    write_json(LEDGER, led)
    log("  cash $%.2f, %d open, %d closed trades" % (led["cash"], len(led["positions"]), len(led["trades"])))
    return 0


if __name__ == "__main__":
    sys.exit(main())
