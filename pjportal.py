"""PJ-Portal availability checker.

One-shot script:
  1. Load config (env file + optional systemd-creds).
  2. Log in / reuse cookie.
  3. Fetch the Merkliste, parse it.
  4. For each configured (tag, hospital, term) combo, check free slots
     and send an ntfy push if any are open.

Designed to be re-fired every 60–360 s by a systemd timer.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import time
from typing import Optional

import requests
from lxml import html

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S%z",
)
log = logging.getLogger("pjportal")

ENV_FILE = "/etc/pjportal.env"
PORTAL_ROOT = "https://www.pj-portal.de"
TERMS = ("first_term", "second_term", "third_term")
REQUIRED_KEYS = ("pjportal_user", "pjportal_pwd", "ajax_uid",
                 "pj_tag", "hospital", "term")
OPTIONAL_KEYS = ("ntfy_url_topic", "cookie_filepath")

# The class name Slot cells always contain. We match on substring so extra
# whitespace or reordered modifier tokens (buchungsphase, ausgebucht, …)
# don't cause silent misses.
SLOT_CELL_MARKER = "tertial_verfuegbarkeit"

ENV: dict[str, str] = {}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_env_file(path: str = ENV_FILE) -> None:
    """Load KEY=value pairs from *path* into os.environ.

    - Existing environ values win (systemd EnvironmentFile= is already applied).
    - Blank values are skipped so an empty placeholder doesn't clobber the
      real one supplied elsewhere.
    """
    if not os.path.exists(path):
        log.debug("No env file at %s", path)
        return
    if not os.access(path, os.R_OK):
        log.warning("%s not readable by uid=%d — try: sudo chmod 640 %s",
                    path, os.getuid(), path)
        return
    count = 0
    with open(path) as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val
                count += 1
    log.info("Loaded %d keys from %s", count, path)


def _read_credential(name: str) -> str:
    """Read *name* from $CREDENTIALS_DIRECTORY (systemd-creds), falling back
    to os.environ."""
    creds_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
    if creds_dir:
        path = os.path.join(creds_dir, name)
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    return os.environ.get(name, "")


def load_config() -> None:
    """Populate the module-level ENV dict and validate required fields.

    Raises SystemExit with a user-actionable message if anything is missing.
    """
    global ENV
    ENV = {k: _read_credential(k) for k in REQUIRED_KEYS + OPTIONAL_KEYS}

    log.info("ntfy_url_topic = %r", ENV["ntfy_url_topic"])
    log.info("pjportal_user  = %r", ENV["pjportal_user"])
    log.info("ajax_uid       = %r", ENV["ajax_uid"])
    log.info("pj_tag         = %r", ENV["pj_tag"])
    log.info("hospital       = %r", ENV["hospital"])
    log.info("term           = %r", ENV["term"])
    log.info("cookie_filepath= %r", ENV["cookie_filepath"])

    missing = [k for k in REQUIRED_KEYS if not ENV[k]]
    if missing:
        _die_missing_config(missing)

    cf = ENV["cookie_filepath"]
    if cf and os.path.exists(cf):
        with open(cf) as f:
            ENV["cookie"] = f.read().strip()
        log.info("Loaded cookie from %s: %s…", cf, ENV["cookie"][:6])
    else:
        ENV["cookie"] = ""
        log.info("No cookie file found, will authenticate fresh")


def _die_missing_config(missing: list[str]) -> None:
    log.error("Missing required config: %s", ", ".join(missing))
    if "pjportal_pwd" in missing:
        log.error("")
        log.error("pjportal_pwd is supplied by systemd-creds when the service runs.")
        log.error("Running manually? Either:")
        log.error("  a) start via systemd:  sudo systemctl start pjportal.service")
        log.error("  b) export it inline:   sudo -E pjportal_pwd='…' python3 pjportal.py")
        log.error("  c) decrypt it once:    sudo systemd-creds decrypt "
                  "/etc/pjportal/pjportal_pwd.cred - | "
                  "sudo tee /run/pjp_pwd >/dev/null && "
                  "pjportal_pwd=\"$(sudo cat /run/pjp_pwd)\" python3 pjportal.py")
    sys.exit(2)


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(msg: str) -> None:
    url = ENV.get("ntfy_url_topic", "")
    log.info("notify() — ntfy_url_topic=%r, msg=%r", url, msg)
    if not url:
        log.error("ntfy_url_topic is empty — set it in %s", ENV_FILE)
        return
    try:
        resp = requests.post(
            url,
            data=msg.encode("utf-8"),
            headers={"Title": "PJ-Portal slot found!", "Priority": "5"},
            timeout=10,
        )
    except Exception as e:
        log.error("ntfy request raised: %s", e)
        return
    if resp.status_code == 200:
        log.info("ntfy: sent OK (200)")
    else:
        log.error("ntfy FAILED: status=%d body=%r", resp.status_code, resp.text)


# ---------------------------------------------------------------------------
# State files (start-ping stamp, failure-alert throttle)
# ---------------------------------------------------------------------------
#
# We keep two tiny marker files next to the cookie:
#
#   started.stamp  — exists ⇒ we've already sent the "bot armed" ping for
#                    this deploy. install.sh deletes it so a redeploy triggers
#                    a fresh confirmation on the next run.
#
#   failure.stamp  — unix-timestamp of the last failure notification. Used
#                    to throttle failure pushes to at most one every
#                    FAILURE_QUIET_HOURS. Deleted on the next successful run,
#                    which also sends a one-shot "recovered" ping.

FAILURE_QUIET_HOURS = 6
DEFAULT_STATE_DIR = "/var/lib/pjportal"


def _state_dir() -> str:
    cf = ENV.get("cookie_filepath", "")
    return os.path.dirname(cf) if cf else DEFAULT_STATE_DIR


def _state_path(name: str) -> str:
    return os.path.join(_state_dir(), name)


def send_start_ping_if_first_run() -> None:
    stamp = _state_path("started.stamp")
    if os.path.exists(stamp):
        return
    log.info("First run since (re)deploy — sending 'bot armed' confirmation ping.")
    notify("PJ-Portal bot armed. You'll only hear from me again when a real "
           "slot opens (or if the bot breaks).")
    # Write the stamp unconditionally so a broken ntfy URL doesn't loop and
    # spam the (working) portal side every 5 minutes. --test-notify remains
    # the way to verify the push pipeline itself.
    try:
        with open(stamp, "w") as f:
            f.write(str(int(time.time())))
    except OSError as e:
        log.warning("Could not write %s: %s", stamp, e)


def report_failure(err: str) -> None:
    """Send at most one failure push per FAILURE_QUIET_HOURS window."""
    stamp = _state_path("failure.stamp")
    now = int(time.time())
    last = 0
    if os.path.exists(stamp):
        try:
            with open(stamp) as f:
                last = int((f.read().strip() or "0"))
        except (OSError, ValueError):
            last = 0
    quiet_secs = FAILURE_QUIET_HOURS * 3600
    if last and now - last < quiet_secs:
        mins_left = (quiet_secs - (now - last)) // 60
        log.info("Failure already reported %.1fh ago — staying quiet for "
                 "~%d more min.", (now - last) / 3600, mins_left)
        return
    notify(f"PJ-Portal bot ERROR: {err}. "
           f"Check `journalctl -u pjportal -n 40 --no-pager`.")
    try:
        with open(stamp, "w") as f:
            f.write(str(now))
    except OSError as e:
        log.warning("Could not write %s: %s", stamp, e)


def clear_failure_state_if_set() -> None:
    """On a successful run, send a one-shot 'recovered' ping if we were
    previously in a failing state, then clear the stamp."""
    stamp = _state_path("failure.stamp")
    if not os.path.exists(stamp):
        return
    log.info("Previous run had alerted about a failure — sending recovery ping.")
    notify("PJ-Portal bot recovered — checks are running normally again.")
    try:
        os.remove(stamp)
    except OSError as e:
        log.warning("Could not remove %s: %s", stamp, e)


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def save_cookie(value: str) -> None:
    cf = ENV.get("cookie_filepath", "")
    if not cf or not value:
        return
    try:
        with open(cf, "w") as f:
            f.write(value)
    except OSError as e:
        log.warning("Could not persist cookie to %s: %s", cf, e)
        return
    ENV["cookie"] = value
    log.info("Saved cookie to %s", cf)


# ---------------------------------------------------------------------------
# Portal interaction
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    s = requests.Session()
    s.headers.update({
        "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) "
                       "Chrome/132.0.0.0 Safari/537.36"),
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


def authenticate(session: requests.Session) -> None:
    log.info("Fetching initial session cookie…")
    session.get(f"{PORTAL_ROOT}/")
    init = session.cookies.get_dict().get("PHPSESSID")
    if init:
        save_cookie(init)

    log.info("Logging in as %s", ENV["pjportal_user"])
    session.headers.update({
        "Origin": PORTAL_ROOT,
        "Referer": f"{PORTAL_ROOT}/index_uu.php",
    })
    session.post(f"{PORTAL_ROOT}/index_uu.php", data={
        "name_Login": "Login",
        "USER_NAME": ENV["pjportal_user"],
        "PASSWORT": ENV["pjportal_pwd"],
        "form_login_submit": "anmelden",
    })
    auth_cookie = session.cookies.get_dict().get("PHPSESSID")
    if auth_cookie:
        save_cookie(auth_cookie)
        log.info("Login OK, new cookie saved")
    else:
        log.warning("Login response had no PHPSESSID — credentials may be wrong")


def fetch_merkliste(session: requests.Session) -> requests.Response:
    log.info("Fetching Merkliste…")
    cookie = ENV.get("cookie", "")
    if cookie:
        session.cookies.set("PHPSESSID", cookie)
        log.info("Using cookie: %s…", cookie[:6])
    else:
        log.info("No cookie set")

    session.headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "de-DE,de;q=0.9",
        "Origin": PORTAL_ROOT,
        "Referer": f"{PORTAL_ROOT}/index_uu.php?PAGE_ID=101",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    r = session.post(f"{PORTAL_ROOT}/ajax.php", data={
        "AJAX_ID": ENV["ajax_uid"],
        "TAB_ID": "Tab_Merkliste",
    })
    log.info("AJAX status=%d bytes=%d", r.status_code, len(r.content))
    log.info("AJAX body preview: %r", r.content[:300].decode("utf-8", errors="replace"))

    bad = '{"HTML":" Antwort kein Handler ","ERRORCLASS":2}'
    if r.status_code != 200 or r.text == bad:
        raise RuntimeError(f"AJAX rejected: status={r.status_code} body={r.text[:200]!r}")
    return r


# ---------------------------------------------------------------------------
# HTML parsing
# ---------------------------------------------------------------------------

_SLOT_RE = re.compile(r"(\d+)\s*/\s*(\d+)")


def _parse_slot_text(raw: str) -> Optional[tuple[int, int]]:
    """Convert the text of a term cell to (free, total).

    Returns None when the cell is genuinely empty (not in booking phase yet).
    'Tertial beendet' / 'ausgebucht' with no numbers → (0, 0).
    """
    if not raw:
        return None
    m = _SLOT_RE.search(raw)
    if m:
        return (int(m.group(1)), int(m.group(2)))
    low = raw.lower()
    if "beendet" in low or "ausgebucht" in low:
        return (0, 0)
    return None


def parse_merkliste(response: requests.Response) -> dict:
    """Parse the Merkliste AJAX response into
        { tag: { hospital: { term: (free, total) | None } } }
    """
    try:
        body = response.json()
    except Exception as e:
        raise RuntimeError(f"Response is not JSON: {e}. Body: {response.text[:300]!r}")

    html_str = body.get("HTML", "")
    if not html_str:
        raise RuntimeError(f"JSON has no HTML key. Keys={list(body.keys())} "
                           f"Body={response.text[:300]!r}")

    log.info("HTML payload: %d chars", len(html_str))
    tree = html.fromstring(html_str)
    rows = tree.xpath("/html/body/table/tr")
    log.info("Parsed %d <tr> rows", len(rows))

    result: dict[str, dict[str, dict[str, Optional[tuple[int, int]]]]] = {}
    current_tag: str = ""
    for row in rows:
        row_cls = row.attrib.get("class", "")

        # --- specialty group header row: "merkliste pj_info_fach ..."
        if "pj_info_fach" in row_cls:
            current_tag = _extract_specialty_name(row)
            if current_tag:
                result.setdefault(current_tag, {})
            continue

        # --- hospital row: "merkliste_krankenhaus"
        if "merkliste_krankenhaus" in row_cls and current_tag:
            hospital_name = ""
            term_idx = 0
            for td in row.xpath(".//td"):
                td_cls = td.attrib.get("class", "")

                if "pj_info_bezeichnung_krankenhaus" in td_cls:
                    hospital_name = _extract_hospital_name(td)
                    if hospital_name:
                        result[current_tag][hospital_name] = {t: None for t in TERMS}
                    continue

                if hospital_name and SLOT_CELL_MARKER in td_cls and term_idx < len(TERMS):
                    raw = " ".join(t.strip() for t in td.xpath(".//text()") if t.strip())
                    result[current_tag][hospital_name][TERMS[term_idx]] = _parse_slot_text(raw)
                    term_idx += 1

    if not result:
        log.warning("Parsed result is EMPTY — session is probably not authenticated")
    else:
        log.info("Found %d specialty groups: %s", len(result), list(result.keys()))
    return result


def _extract_specialty_name(row) -> str:
    """The specialty name lives in a <td class=" "> (single-space class).
    Fall back to any td whose stripped class is empty."""
    for td in row.xpath(".//td"):
        if td.attrib.get("class", "").strip() == "":
            texts = [t.strip() for t in td.xpath(".//text()") if t.strip()]
            if texts:
                return texts[0]
    return ""


def _extract_hospital_name(td) -> str:
    """Hospital name is the third non-empty text node in the cell (after
    icons/spacers). Fall back to the last one if the layout drifts."""
    texts = [t.strip() for t in td.xpath(".//text()") if t.strip()]
    if not texts:
        return ""
    if len(texts) >= 3:
        return texts[2]
    return texts[-1]


# ---------------------------------------------------------------------------
# Slot matching
# ---------------------------------------------------------------------------

def _split_csv(key: str) -> list[str]:
    return [x.strip() for x in ENV[key].split(",") if x.strip()]


def check_slots(merkliste: dict) -> None:
    tags      = _split_csv("pj_tag")
    hospitals = _split_csv("hospital")
    terms     = _split_csv("term")
    log.info("Checking %d tag(s) × %d hospital(s) × %d term(s)",
             len(tags), len(hospitals), len(terms))

    available_tags = list(merkliste.keys())

    for tag in tags:
        if tag not in merkliste:
            log.info("  %s: not in Merkliste. Available on your Merkliste: %s",
                     tag, available_tags or "(none)")
            continue
        hospitals_for_tag = list(merkliste[tag].keys())
        for hosp in hospitals:
            if hosp not in merkliste[tag]:
                log.info("  %s / %s: hospital not in Merkliste. "
                         "Available for %s: %s",
                         tag, hosp, tag, hospitals_for_tag or "(none)")
                continue
            for term in terms:
                slots = merkliste[tag][hosp].get(term)
                if slots is None:
                    log.info("  %s / %s / %s: not in booking phase", tag, hosp, term)
                    continue
                free, total = slots
                log.info("  %s / %s / %s: %d/%d", tag, hosp, term, free, total)
                if free > 0:
                    msg = (f"SLOT AVAILABLE: {tag} at {hosp} ({term}) — "
                           f"{free}/{total} free!")
                    log.info("  *** %s", msg)
                    notify(msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run_once() -> None:
    """Try the Merkliste once with the cached cookie, re-auth on any failure."""
    session = make_session()
    try:
        r = fetch_merkliste(session)
        merkliste = parse_merkliste(r)
        if not merkliste:
            raise RuntimeError("empty merkliste on first attempt")
        check_slots(merkliste)
        return
    except Exception as e:
        log.warning("First attempt failed: %s — re-authenticating", e)

    session = make_session()
    authenticate(session)
    r = fetch_merkliste(session)
    merkliste = parse_merkliste(r)
    if not merkliste:
        raise RuntimeError("merkliste still empty after re-auth — "
                           "check credentials or ajax_uid")
    check_slots(merkliste)


def main(argv: list[str]) -> int:
    load_env_file()
    log.info("=" * 50)

    if "--test-notify" in argv:
        log.info("TEST-NOTIFY MODE")
        load_config()
        notify("PJ-Portal bot test — notification pipeline works!")
        return 0

    load_config()
    send_start_ping_if_first_run()

    log.info("Starting check")
    try:
        run_once()
    except Exception as e:
        log.error("Check failed: %s", e, exc_info=True)
        report_failure(str(e))
        return 1
    clear_failure_state_if_set()
    log.info("Check complete")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
