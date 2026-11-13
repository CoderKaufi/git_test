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
import base64
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


CZ_DAYS = ["pondělí", "úterý", "středa", "čtvrtek", "pátek", "sobota", "neděle"]
CZ_MONTHS = ["ledna", "února", "března", "dubna", "května", "června", "července", "srpna",
             "září", "října", "listopadu", "prosince"]
CZ_REASONS = {"stop": "stop-loss", "target": "cíl (2R)", "session ended": "konec obchodního dne"}


def cz_money(x, sign=False):
    """10000.5 -> '10 000,50 USD' (Czech style); sign=True adds + or −."""
    txt = format(abs(x), ",.2f").replace(",", " ").replace(".", ",")
    pre = ("+" if x > 0 else "−" if x < 0 else "") if sign else ("−" if x < 0 else "")
    return "%s%s USD" % (pre, txt)


def cz_num(x, digits=2, sign=False):
    txt = format(abs(x), ",.%df" % digits).replace(",", " ").replace(".", ",")
    return (("+" if x > 0 else "−" if x < 0 else "") if sign else ("−" if x < 0 else "")) + txt


def cz_date(d):
    return "%s %d. %s %d" % (CZ_DAYS[d.weekday()], d.day, CZ_MONTHS[d.month - 1], d.year)


def cz_plural(n, one, few, many):
    return one if n == 1 else few if 2 <= n <= 4 else many


def build_report(led, today):
    """The day's statement, in Czech. Returns (text, day P&L, account value)."""
    cfg = read_json(CONFIG)
    d = today.isoformat()
    todays = [t for t in led["trades"] if t["date"] == d]
    day_pnl = round(sum(t["pnl"] for t in todays), 2)
    eq = equity(led)
    start = led["starting_cash"]
    all_t = led["trades"]
    wins = [t for t in all_t if t["pnl"] > 0]
    started = dt.date.fromisoformat(led.get("started") or d)
    lines = ["Výpis z cvičného účtu: %s" % cz_date(today),
             "(Fiktivní peníze, nic nebylo odesláno brokerovi.)", "",
             "Hodnota účtu:       %s" % cz_money(eq),
             "Dnešní výsledek:    %s" % cz_money(day_pnl, sign=True),
             "Celkový výsledek:   %s (%s %%) od %d. %d. %d" % (cz_money(eq - start, sign=True),
                 cz_num((eq - start) / start * 100, sign=True), started.day, started.month, started.year),
             "Počáteční vklad:    %s" % cz_money(start),
             "Hotovost:           %s" % cz_money(led["cash"]), ""]
    if todays:
        lines.append("Dnešní obchody (newyorský čas; prodej = 5minutová svíčka, ve které se cena dotkla):")
        for t in todays:
            reason = CZ_REASONS.get(t["exit_reason"]) or (
                "uzavřeno v %s" % t["exit_reason"].split()[-1] if t["exit_reason"].startswith("flat by")
                else t["exit_reason"])
            lines.append("  %s  %d ks: nákup v %s za %s, prodej v %s za %s (%s)"
                         % (t["symbol"], t["qty"], t["entry_time"], cz_num(t["entry"]), t["exit_time"],
                            cz_num(t["exit"]), reason))
            lines.append("      výsledek %s, %sR" % (cz_money(t["pnl"], sign=True), cz_num(t["R"] or 0, sign=True)))
    else:
        lines.append("Dnes žádné obchody: žádný signál neprošel všemi pravidly.")
    if led["positions"]:
        lines += ["", "Stále otevřené pozice (uzavřou se při dalším spuštění):"]
        for p in led["positions"]:
            lines.append("  %s %d ks za %s" % (p["symbol"], p["qty"], cz_num(p["entry"])))
    n = len(all_t)
    lines += ["", "Celkem: %d %s, z toho %d %s (%s %%), průměr %sR"
              % (n, cz_plural(n, "obchod", "obchody", "obchodů"), len(wins),
                 cz_plural(len(wins), "ziskový", "ziskové", "ziskových"),
                 cz_num(len(wins) / n * 100 if n else 0, 0), cz_num(sum(t["R"] or 0 for t in all_t) / n if n else 0, sign=True)),
              "(R = riziko obchodu, tedy vzdálenost od nákupu ke stop-lossu; +2R = cíl, −1R = stop-loss.)"]
    syms = sorted({t["symbol"] for t in todays})
    if syms:
        code, news, err = skill("trader-news/news.py", "--tickers", *syms, "--no-sec", "--dry-run",
                                "--hours", "24", "--all")
        items = (news or {}).get("items") or []
        if items:
            lines += ["", "Zprávy k dnešním akciím (anglicky, Yahoo Finance):"]
            per = {}
            for it in items:
                tk = ",".join(it.get("tickers", [])) or it.get("ticker", "")
                if per.get(tk, 0) >= 3:
                    continue
                per[tk] = per.get(tk, 0) + 1
                lines.append("  %s: %s" % (tk, it.get("title", "")))
    lines += ["", "Pravidla: vklad %s, max. ztráta %s na obchod a %s za den, nejvýše %d obchody denně. "
              "Strategie: průraz ranního rozpětí (prvních 15 minut), stop-loss ve středu rozpětí, cíl 2R, "
              "nákupy 9:50–15:00 a vše prodáno do 15:55 newyorského času."
              % (cz_money(start), cz_money(cfg["max_loss_per_trade_usd"]), cz_money(cfg["max_daily_loss_usd"]),
                 cfg["max_trades_per_day"]),
              "Slouží ke vzdělávání, nejde o investiční doporučení."]
    return "\n".join(lines) + "\n", day_pnl, eq


def report_title(today, day_pnl, eq, simulated=False):
    return "%sCvičný účet %d. %d. %d: dnes %s, účet %s" % (
        "SIMULACE – " if simulated else "", today.day, today.month, today.year,
        cz_money(day_pnl, sign=True), cz_money(eq))


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
                                 headers={"Title": "=?UTF-8?B?%s?=" % base64.b64encode(title.encode()).decode(),
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
    ap.add_argument("--simulate", action="store_true", help="mark the report as a simulation (for --now replays)")
    a = ap.parse_args(argv)
    if a.test_notify:
        ok = deliver("Obchodní bot: zkouška", "Pokud tohle čtete v telefonu, denní výpis z cvičného účtu "
                     "vám sem přijde po každém obchodním dni.")
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
        subject = report_title(today, day_pnl, eq, simulated=a.simulate)
        if a.simulate:
            body = ("SIMULACE: zkušební přehrání dne %d. %d. %d na cvičném účtu, ne skutečný výpis.\n\n"
                    % (today.day, today.month, today.year)) + body
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
