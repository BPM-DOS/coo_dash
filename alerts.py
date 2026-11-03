"""
alerts.py - error emails + dead man's switch heartbeat for BPM-DOS sync jobs.

Stdlib only. The SAME file lives in two repos; keep them identical:
    BPM-DOS/etl_pipeline   sync/alerts.py
    BPM-DOS/coo_dash       alerts.py
After editing either copy, copy it over and compare ALERTS_VERSION / sha256.

Usage
-----
    from sync import alerts            # etl_pipeline
    import alerts                      # coo_dash

    monitor = alerts.start("COO Dash - live metrics", heartbeat_env="COO_LIVE_HEARTBEAT_URL")
    ...
    try:
        risky()
    except Exception as e:
        alerts.error("Google Reviews", "sync failed", exc=e)   # handled: run continues
    ...
    sys.exit(monitor.finish())         # 0 = clean, 1 = had errors

What it catches
---------------
* Handled errors     - anything passed to alerts.error() during the run.
* Crashes            - uncaught exceptions (sys.excepthook).
* Killed runs        - SIGTERM, e.g. `timeout` or `docker stop` ending a hung run.
* Forgotten finish() - an atexit hook finishes the run.
A run that never reports at all (VM down, container OOM-killed, image pull
failed, cron line broken) cannot be detected from inside Python. That is what
the heartbeat is for: the external monitor emails when the pings stop.

Emails (to ALERT_EMAIL_TO, default bpm-dos@bradenproperty.com)
------
    [ETL FAILED] <job> - <n> errors: <where>, ...
    [ETL RECOVERED] <job>
A FAILED email goes out the first time a run records an error. The exact same
set of errors is re-sent at most every ALERT_REPEAT_HOURS (default 6); a job
on a 30-minute cron would otherwise send 48 identical emails a day. Any NEW
error emails immediately, and the first clean run afterwards sends RECOVERED.
ALERT_REPEAT_HOURS=0 emails on every failing run.

Heartbeat (Healthchecks.io-style ping URL, read from the env var named by heartbeat_env)
---------
    <url>/start   run started
    <url>         run reached the end (the body says if it had handled errors)
    <url>/fail    run crashed or was killed
Handled errors still ping success on purpose: the heartbeat answers "is the
job alive and finishing?", the emails answer "did anything go wrong?".

Environment
-----------
    SMTP_HOST, SMTP_PORT (587), SMTP_USERNAME, SMTP_PASSWORD
    SMTP_USE_SSL (false), SMTP_USE_TLS (true unless SSL)
    ALERT_EMAIL_FROM     falls back to FAILURE_EMAIL_FROM, then SMTP_USERNAME
    ALERT_EMAIL_TO       falls back to FAILURE_EMAIL_TO, then bpm-dos@bradenproperty.com
    ALERT_REPEAT_HOURS   6
    ALERT_STATE_DIR      folder for the small per-job state file (overrides the job's default)
    ALERT_LOG_LINES      60 - log tail included in emails
    ALERT_DRY_RUN=1      print emails / pings instead of sending them (local testing)
    ALERTS_DISABLED=1    no emails, no pings
"""
from __future__ import annotations

import atexit
import collections
import hashlib
import json
import os
import re
import signal
import smtplib
import socket
import sys
import threading
import time
import traceback
import urllib.request
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage

ALERTS_VERSION = "2026-11-03"
DEFAULT_TO = "bpm-dos@bradenproperty.com"
SUBJECT_FAILED = "[ETL FAILED]"
SUBJECT_RECOVERED = "[ETL RECOVERED]"

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:  # no tz database available: show UTC only
    _ET = None

_current = None  # the active RunMonitor, if any


class RunTerminated(BaseException):
    """Raised by the SIGTERM handler. BaseException on purpose, so the job's own
    `except Exception:` blocks can't swallow it and keep a killed run going."""


# ---------------------------------------------------------------- helpers

def _env(name, default=None):
    if not name:
        return default
    value = os.environ.get(name)
    if value is None:
        return default
    value = value.strip()
    return value if value else default


def _env_bool(name, default=False):
    value = _env(name)
    if value is None:
        return default
    return value.lower() in ("1", "true", "yes", "y", "on")


def _env_float(name, default):
    try:
        return float(_env(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def _now():
    return datetime.now(timezone.utc)


def _parse_iso(value):
    try:
        return datetime.fromisoformat(value) if value else None
    except (TypeError, ValueError):
        return None


def _fmt(dt):
    if dt is None:
        return "?"
    if _ET is not None:
        return dt.astimezone(_ET).strftime("%Y-%m-%d %H:%M:%S %Z")
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def _duration(seconds):
    seconds = int(max(0, seconds))
    hours, rem = divmod(seconds, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours}h {minutes}m {secs}s" if hours else f"{minutes}m {secs}s"


def _oneline(text, limit=None):
    text = " ".join(str(text).split())
    if limit and len(text) > limit:
        text = text[: limit - 3] + "..."
    return text


def _indent(text, prefix="   ", max_lines=30, max_line_chars=300):
    lines = str(text).rstrip("\n").splitlines()
    if len(lines) > max_lines:
        lines = [f"... ({len(lines) - max_lines} earlier lines cut)"] + lines[-max_lines:]
    return "\n".join(prefix + (ln if len(ln) <= max_line_chars else ln[:max_line_chars] + "...") for ln in lines)


_NORMALIZE = [
    (re.compile(r"\brec[a-z0-9]{14}\b"), "rec*"),
    (re.compile(r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b"), "<uuid>"),
    (re.compile(r"0x[0-9a-f]+"), "0x*"),
    (re.compile(r"\d+"), "#"),
]


def _fingerprint(job, where, message):
    """Stable id for 'the same problem' across runs. Numbers and record ids are
    ignored, so 'batch 3 failed' and 'batch 7 failed' count as the same error."""
    text = f"{where}|{message}".lower()[:300]
    for pattern, replacement in _NORMALIZE:
        text = pattern.sub(replacement, text)
    return hashlib.sha1(f"{job}|{text}".encode("utf-8")).hexdigest()[:12]


def _job_frame(tb):
    """Innermost traceback frame that belongs to the job, not to a library."""
    try:
        frames = traceback.extract_tb(tb)
    except Exception:
        return None
    this_file = os.path.basename(__file__)
    own = [f for f in frames if "site-packages" not in f.filename
           and os.path.basename(f.filename) != this_file
           and "dist-packages" not in f.filename
           and not f.filename.startswith(("/usr/lib/python", "/usr/local/lib/python", "<"))]
    pick = (own or frames or [None])[-1]
    return pick


class _Tee:
    """Pass-through wrapper for stdout/stderr that remembers recent lines."""

    def __init__(self, stream, tail, lock):
        self._stream = stream
        self._tail = tail
        self._lock = lock
        self._partial = ""

    def write(self, s):
        written = self._stream.write(s)
        try:
            if isinstance(s, str) and s:
                with self._lock:
                    lines = (self._partial + s).split("\n")
                    self._partial = lines.pop()
                    if len(self._partial) > 4000:
                        lines.append(self._partial)
                        self._partial = ""
                    self._tail.extend(lines)
        except Exception:
            pass
        return written

    def flush(self):
        return self._stream.flush()

    def __getattr__(self, name):
        return getattr(self._stream, name)


# ---------------------------------------------------------------- monitor

class RunMonitor:
    def __init__(self, job, heartbeat_env=None, state_dir=None, runbook=None, capture_output=True):
        self.job = str(job)
        self.slug = re.sub(r"[^a-z0-9]+", "-", self.job.lower()).strip("-") or "job"
        self.runbook = runbook
        self.started_at = _now()
        self.finished_at = None
        self.errors = []
        self.crashed = False
        self.disabled = _env_bool("ALERTS_DISABLED")
        self.dry_run = _env_bool("ALERT_DRY_RUN")
        self.repeat_hours = _env_float("ALERT_REPEAT_HOURS", 6)
        self.heartbeat_env = heartbeat_env
        self.heartbeat_url = (_env(heartbeat_env) or "").rstrip("/")
        folder = _env("ALERT_STATE_DIR") or state_dir or os.getcwd()
        self.state_path = os.path.join(folder, f".alert_state.{self.slug}.json")
        self._lock = threading.RLock()
        self._tail = collections.deque(maxlen=max(0, int(_env_float("ALERT_LOG_LINES", 60))))
        self._tees = []
        self._finished = False
        self._exit_code = None
        self._prev_excepthook = None
        if capture_output:
            self._install_tees()

    # ---- public

    def error(self, where, message="", exc=None, detail=None):
        """Record a handled error. The run keeps going; finish() reports it."""
        where = _oneline(where or "unknown", 120)
        message = str(message or "").strip()
        if exc is not None:
            exc_text = f"{type(exc).__name__}: {exc}".strip()
            message = f"{message}: {exc_text}" if message else exc_text
            if detail is None and getattr(exc, "__traceback__", None) is not None:
                detail = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
        message = _oneline(message or "error", 1000)
        fp = _fingerprint(self.job, where, message)
        with self._lock:
            for existing in self.errors:
                if existing["fingerprint"] == fp:
                    existing["count"] += 1
                    break
            else:
                self.errors.append({
                    "where": where, "message": message, "detail": detail,
                    "fingerprint": fp, "count": 1, "at": _now().isoformat(),
                })
        self._log(f"ERROR recorded - {where}: {message}")

    @property
    def has_errors(self):
        return bool(self.errors)

    def finish(self):
        """End the run: ping the heartbeat and send email if needed.
        Returns the exit code (0 clean, 1 errors). Safe to call more than once."""
        with self._lock:
            if self._finished:
                return self._exit_code
            self._finished = True
        self.finished_at = _now()
        ok = not self.errors
        self._exit_code = 0 if ok else 1
        try:
            self._heartbeat_finish(ok)
        except Exception as e:
            self._log(f"heartbeat step failed: {e}")
        try:
            self._notify(ok)
        except Exception as e:
            self._log(f"email step failed: {type(e).__name__}: {e}")
        took = _duration((self.finished_at - self.started_at).total_seconds())
        self._log(f"run finished in {took} - {'clean' if ok else str(len(self.errors)) + ' error(s)'}"
                  f"{' (crashed)' if self.crashed else ''}")
        self._uninstall_tees()
        return self._exit_code

    # ---- hooks

    def _install_hooks(self):
        self._prev_excepthook = sys.excepthook
        sys.excepthook = self._excepthook
        atexit.register(self._atexit)
        try:
            if threading.current_thread() is threading.main_thread() and \
                    signal.getsignal(signal.SIGTERM) in (signal.SIG_DFL, None):
                signal.signal(signal.SIGTERM, _raise_terminated)
        except Exception:
            pass

    def _excepthook(self, exc_type, exc, tb):
        try:
            self._crash(exc_type, exc, tb)
        except Exception as e:
            print(f"[alerts] crash handling failed: {e}", file=sys.__stderr__)
        (self._prev_excepthook or sys.__excepthook__)(exc_type, exc, tb)

    def _atexit(self):
        if not self._finished:
            self._log("process is exiting without finish() - finishing the run now")
            self.finish()

    def _crash(self, exc_type, exc, tb):
        if self._finished:
            return
        self.crashed = True
        frame = _job_frame(tb)
        where = f"crash in {os.path.basename(frame.filename)}:{frame.lineno}" if frame else "crash"
        detail = "".join(traceback.format_exception(exc_type, exc, tb))
        self.error(where, f"{exc_type.__name__}: {exc}", detail=detail)
        self.finish()

    # ---- output capture

    def _install_tees(self):
        for name in ("stdout", "stderr"):
            stream = getattr(sys, name, None)
            if stream is None or isinstance(stream, _Tee):
                continue
            setattr(sys, name, _Tee(stream, self._tail, self._lock))
            self._tees.append((name, stream))

    def _uninstall_tees(self):
        for name, stream in self._tees:
            if isinstance(getattr(sys, name, None), _Tee):
                setattr(sys, name, stream)
        self._tees = []

    def _log(self, msg):
        try:
            print(f"[alerts] {msg}", flush=True)
        except Exception:
            pass

    # ---- heartbeat

    def _ping(self, suffix, body=""):
        if self.disabled or not self.heartbeat_url:
            return False
        label = suffix or "/(success)"
        if self.dry_run:
            self._log(f"[dry-run] heartbeat ping {label}: {_oneline(body, 200)}")
            return True
        data = str(body).encode("utf-8", "replace")[:20000]
        last = None
        for attempt in range(3):
            try:
                req = urllib.request.Request(
                    self.heartbeat_url + suffix, data=data, method="POST",
                    headers={"User-Agent": f"bpm-dos-alerts/{ALERTS_VERSION}"})
                with urllib.request.urlopen(req, timeout=10) as resp:
                    resp.read()
                return True
            except Exception as e:
                last = e
                time.sleep(1 + attempt)
        self._log(f"heartbeat ping {label} failed: {type(last).__name__}: {last}")
        return False

    def _heartbeat_finish(self, ok):
        took = _duration((self.finished_at - self.started_at).total_seconds())
        if self.crashed:
            self._ping("/fail", f"{self.job} crashed after {took}.\n\n" + self._error_lines())
        elif ok:
            self._ping("", f"{self.job} finished clean in {took}.")
        else:
            self._ping("", f"{self.job} finished in {took} with {len(self.errors)} handled error(s):\n"
                           + self._error_lines())

    def _error_lines(self):
        return "\n".join(f"- {e['where']}: {_oneline(e['message'], 300)}" for e in self.errors)

    # ---- email

    def _load_state(self):
        try:
            with open(self.state_path, "r", encoding="utf-8") as f:
                data = json.load(f)
            return data if isinstance(data, dict) else {}
        except FileNotFoundError:
            return {}
        except Exception as e:
            self._log(f"could not read {self.state_path} ({e}); treating as a fresh start")
            return {}

    def _save_state(self, data):
        if self.dry_run:
            return  # a dry run must not mute the next real email
        try:
            os.makedirs(os.path.dirname(self.state_path) or ".", exist_ok=True)
            tmp = self.state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(data, f, indent=2, sort_keys=True)
            os.replace(tmp, self.state_path)
        except Exception as e:
            self._log(f"could not write {self.state_path} ({e}); repeat muting won't work until this is fixed")

    def _notify(self, ok):
        if self.disabled:
            return
        state = self._load_state()
        open_issues = state.get("open") or {}
        now = self.finished_at

        if ok:
            if open_issues:
                self._send(f"{SUBJECT_RECOVERED} {self.job}", self._recovered_body(open_issues))
            if state:
                self._save_state({})
            return

        current = {e["fingerprint"]: e for e in self.errors}
        new_fps = [fp for fp in current if fp not in open_issues]
        resolved = {fp: v for fp, v in open_issues.items() if fp not in current}
        last_email = _parse_iso(state.get("last_email_at"))
        repeat_due = last_email is None or (now - last_email) >= timedelta(hours=self.repeat_hours)

        new_open = {}
        for fp, e in current.items():
            prev = open_issues.get(fp) or {}
            new_open[fp] = {
                "where": e["where"],
                "message": _oneline(e["message"], 300),
                "first_seen": prev.get("first_seen") or now.isoformat(),
                "runs": int(prev.get("runs", 0)) + 1,
            }
        new_state = {"open": new_open, "last_email_at": state.get("last_email_at")}

        if new_fps or repeat_due:
            if self._send(self._failed_subject(), self._failed_body(new_open, set(new_fps), resolved)):
                new_state["last_email_at"] = now.isoformat()
        else:
            next_at = last_email + timedelta(hours=self.repeat_hours)
            self._log(f"same {len(current)} error(s) already emailed at {_fmt(last_email)}; "
                      f"not re-sending before {_fmt(next_at)} (ALERT_REPEAT_HOURS={self.repeat_hours:g})")
        self._save_state(new_state)

    def _failed_subject(self):
        if self.crashed:
            head = "crashed: " + self.errors[-1]["message"]
        else:
            wheres = list(dict.fromkeys(e["where"] for e in self.errors))
            n = len(self.errors)
            head = f"{n} error{'s' if n != 1 else ''}: " + ", ".join(wheres[:4]) + (", ..." if len(wheres) > 4 else "")
        return _oneline(f"{SUBJECT_FAILED} {self.job} - {head}", 180)

    def _header(self):
        took = _duration((self.finished_at - self.started_at).total_seconds())
        return (
            f"Started   {_fmt(self.started_at)}\n"
            f"Ended     {_fmt(self.finished_at)}  ({took})\n"
            f"Host      {socket.gethostname()}\n"
        )

    def _failed_body(self, open_issues, new_fps, resolved):
        n = len(self.errors)
        status = "crashed before finishing" if self.crashed else f"finished with {n} error{'s' if n != 1 else ''}"
        out = [f"{self.job} {status}.", "", self._header(), "ERRORS", "------"]
        for i, e in enumerate(self.errors, 1):
            info = open_issues.get(e["fingerprint"], {})
            tag = "NEW" if e["fingerprint"] in new_fps else "ONGOING"
            times = f" (x{e['count']} this run)" if e["count"] > 1 else ""
            out.append(f"{i}. [{tag}] {e['where']}{times}")
            out.append(_indent(e["message"]))
            if tag == "ONGOING":
                since = _fmt(_parse_iso(info.get("first_seen")))
                out.append(f"   failing since {since} ({info.get('runs', '?')} runs in a row)")
            if e.get("detail"):
                out.append("")
                out.append(_indent(e["detail"], prefix="      ", max_lines=25))
            out.append("")
        if resolved:
            out += ["NOW RESOLVED", "------------"]
            for v in resolved.values():
                out.append(f"- {v.get('where')}: {_oneline(v.get('message', ''), 200)}")
            out.append("")
        tail = [ln for ln in list(self._tail) if ln.strip() and not ln.startswith("[alerts]")]
        if tail:
            out += [f"LAST {len(tail)} LOG LINES", "-----------------"]
            out.append(_indent("\n".join(tail), prefix="", max_lines=len(tail)))
            out.append("")
        out += ["WHAT HAPPENS NEXT", "-----------------",
                f"These same errors won't be emailed again for {self.repeat_hours:g}h; a new error emails right away.",
                f"The first clean run sends {SUBJECT_RECOVERED}."]
        if self.runbook:
            out += ["", "WHERE TO LOOK", "-------------", str(self.runbook)]
        body = "\n".join(out)
        return body[:60000]

    def _recovered_body(self, open_issues):
        firsts = [d for d in (_parse_iso(v.get("first_seen")) for v in open_issues.values()) if d]
        since = _fmt(min(firsts)) if firsts else "?"
        runs = max((int(v.get("runs", 0)) for v in open_issues.values()), default=0)
        out = [f"{self.job} ran clean again.", "", self._header(),
               f"It had been failing since {since} ({runs} failing run{'s' if runs != 1 else ''}).", "",
               "RESOLVED", "--------"]
        for v in open_issues.values():
            out.append(f"- {v.get('where')}: {_oneline(v.get('message', ''), 200)}")
        return "\n".join(out)

    def _send(self, subject, body):
        recipients = [a.strip() for a in (_env("ALERT_EMAIL_TO") or _env("FAILURE_EMAIL_TO") or DEFAULT_TO).split(",") if a.strip()]
        sender = _env("ALERT_EMAIL_FROM") or _env("FAILURE_EMAIL_FROM") or _env("SMTP_USERNAME")
        host = _env("SMTP_HOST")
        if self.dry_run:
            self._log(f"[dry-run] email to {', '.join(recipients)}\nSubject: {subject}\n\n{body}\n[dry-run] end of email")
            return True
        if not host or not sender:
            self._log(f"EMAIL NOT SENT (SMTP_HOST / ALERT_EMAIL_FROM not set): {subject}")
            return False
        msg = EmailMessage()
        msg["From"] = sender
        msg["To"] = ", ".join(recipients)
        msg["Subject"] = subject
        msg["X-BPM-Alert-Job"] = self.slug
        msg.set_content(body)
        port = int(_env_float("SMTP_PORT", 587))
        use_ssl = _env_bool("SMTP_USE_SSL", False)
        use_tls = _env_bool("SMTP_USE_TLS", not use_ssl)
        username, password = _env("SMTP_USERNAME"), _env("SMTP_PASSWORD")
        last = None
        for attempt in range(2):
            try:
                smtp_cls = smtplib.SMTP_SSL if use_ssl else smtplib.SMTP
                with smtp_cls(host, port, timeout=30) as smtp:
                    if use_tls and not use_ssl:
                        smtp.starttls()
                    if username and password:
                        smtp.login(username, password)
                    smtp.send_message(msg)
                self._log(f"emailed {', '.join(recipients)}: {subject}")
                return True
            except Exception as e:
                last = e
                time.sleep(3)
        self._log(f"EMAIL FAILED ({type(last).__name__}: {last}); subject was: {subject}")
        return False


def _raise_terminated(signum, frame):
    raise RunTerminated(f"received signal {signum} (SIGTERM): the run was killed, e.g. by `timeout` or `docker stop`")


# ---------------------------------------------------------------- module API

def start(job, heartbeat_env=None, state_dir=None, runbook=None, capture_output=True, install_hooks=True):
    """Begin monitoring this process's run. Call once, as early as possible."""
    global _current
    if _current is not None and not _current._finished:
        return _current
    monitor = RunMonitor(job, heartbeat_env=heartbeat_env, state_dir=state_dir,
                         runbook=runbook, capture_output=capture_output)
    _current = monitor
    if install_hooks:
        monitor._install_hooks()
    if monitor.disabled:
        email_note, beat_note = "off (ALERTS_DISABLED)", "off (ALERTS_DISABLED)"
    else:
        to = _env("ALERT_EMAIL_TO") or _env("FAILURE_EMAIL_TO") or DEFAULT_TO
        email_note = (f"to {to}" if _env("SMTP_HOST") else "NOT CONFIGURED (SMTP_HOST unset)")
        beat_note = "on" if monitor.heartbeat_url else f"off ({heartbeat_env or 'no env var'} not set)"
        if monitor.dry_run:
            email_note += " [dry-run]"
    monitor._log(f"monitoring '{job}' (alerts {ALERTS_VERSION}) - email: {email_note}; heartbeat: {beat_note}")
    monitor._ping("/start")
    return monitor


def error(where, message="", exc=None, detail=None):
    """Record a handled error on the active run. Safe to call with no active
    run (e.g. a one-off backfill script); then it only prints."""
    monitor = _current
    if monitor is None or monitor._finished:
        extra = f" {type(exc).__name__}: {exc}" if exc is not None else ""
        print(f"[alerts] (no active run) {where}: {message}{extra}", flush=True)
        return
    monitor.error(where, message, exc=exc, detail=detail)


def current():
    return _current


# ---------------------------------------------------------------- test from the command line
#   coo_dash (VM):      cd /root/coo_dash && python3 alerts.py --test
#   etl_pipeline (VM):  docker run --rm --env-file /root/.env bpmdos/etl-pipeline:latest python -m sync.alerts --test
# Sends one real "[ETL FAILED] Alert test" email with the configured SMTP settings
# (no heartbeat ping, no repeat-muting), so you can confirm delivery end to end.

if __name__ == "__main__":
    if "--test" not in sys.argv[1:]:
        print("usage: python alerts.py --test   (sends a test alert email)")
        sys.exit(2)
    try:
        from dotenv import load_dotenv  # picks up the job's .env when run from its folder
        load_dotenv(os.path.join(os.getcwd(), ".env"), override=True)
    except ImportError:
        pass
    os.environ["ALERT_REPEAT_HOURS"] = "0"
    test = RunMonitor("Alert test", state_dir=os.path.join(os.path.expanduser("~"), ".cache"),
                      runbook="This is a test of the BPM-DOS sync alerting. No action needed.")
    test.error("alerts --test", f"test alert sent from {socket.gethostname()} - if you can read this, error emails work")
    test.finish()
    sys.exit(0)
