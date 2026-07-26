import sys
import os
import logging
import requests
from lxml import html

logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S%z",
)

ENV_FILE = "/etc/pjportal.env"
ENV = {}


# ---------------------------------------------------------------------------
# Config loading
# ---------------------------------------------------------------------------

def load_env_file():
    """Load key=value pairs from ENV_FILE into os.environ.
    Only sets keys that are not already present (systemd EnvironmentFile= wins).
    Skips blank values so an empty placeholder line doesn't overwrite a real one.
    """
    if not os.path.exists(ENV_FILE):
        logging.debug(f"No env file at {ENV_FILE}")
        return
    if not os.access(ENV_FILE, os.R_OK):
        logging.warning(f"{ENV_FILE} is not readable by uid={os.getuid()} — run: sudo chmod 640 {ENV_FILE}")
        return
    count = 0
    with open(ENV_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            key = key.strip()
            val = val.strip().strip('"').strip("'")
            if key and val and key not in os.environ:
                os.environ[key] = val
                count += 1
    logging.info(f"Loaded {count} keys from {ENV_FILE}")


def load_config():
    global ENV

    # systemd-creds: password may live in $CREDENTIALS_DIRECTORY/pjportal_pwd
    creds_dir = os.environ.get("CREDENTIALS_DIRECTORY", "")
    def get(name):
        if creds_dir:
            path = os.path.join(creds_dir, name)
            if os.path.exists(path):
                with open(path) as f:
                    return f.read().strip()
        return os.environ.get(name, "")

    ENV = {k: get(k) for k in [
        "pjportal_user", "pjportal_pwd", "ajax_uid",
        "pj_tag", "hospital", "term",
        "ntfy_url_topic",
        "cookie_filepath",
    ]}

    logging.info(f"ntfy_url_topic = {ENV['ntfy_url_topic']!r}")
    logging.info(f"pjportal_user  = {ENV['pjportal_user']!r}")
    logging.info(f"ajax_uid       = {ENV['ajax_uid']!r}")
    logging.info(f"pj_tag         = {ENV['pj_tag']!r}")
    logging.info(f"hospital       = {ENV['hospital']!r}")
    logging.info(f"term           = {ENV['term']!r}")
    logging.info(f"cookie_filepath= {ENV['cookie_filepath']!r}")

    required = ["pjportal_user", "pjportal_pwd", "ajax_uid", "pj_tag", "hospital", "term"]
    missing = [k for k in required if not ENV[k]]
    if missing:
        raise ValueError(f"Missing required config: {', '.join(missing)}")

    # Load persisted cookie
    cf = ENV["cookie_filepath"]
    if cf and os.path.exists(cf):
        with open(cf) as f:
            ENV["cookie"] = f.read().strip()
        logging.info(f"Loaded cookie from {cf}: {ENV['cookie'][:6]}…")
    else:
        ENV["cookie"] = ""
        logging.info("No cookie file found, will authenticate fresh")


# ---------------------------------------------------------------------------
# Notifications
# ---------------------------------------------------------------------------

def notify(msg):
    url = ENV.get("ntfy_url_topic", "")
    logging.info(f"notify() called — ntfy_url_topic={url!r}, msg={msg!r}")
    if not url:
        logging.error("ntfy_url_topic is empty — cannot send notification. Set it in /etc/pjportal.env")
        return
    try:
        resp = requests.post(
            url,
            data=msg.encode("utf-8"),
            headers={"Title": "PJ-Portal slot found!", "Priority": "5"},
            timeout=10,
        )
        logging.info(f"ntfy response: status={resp.status_code} body={resp.text!r}")
        if resp.status_code != 200:
            logging.error(f"ntfy FAILED: status={resp.status_code} body={resp.text!r}")
    except Exception as e:
        logging.error(f"ntfy request raised exception: {e}")


# ---------------------------------------------------------------------------
# Cookie helpers
# ---------------------------------------------------------------------------

def save_cookie(value):
    cf = ENV.get("cookie_filepath", "")
    if not cf:
        return
    with open(cf, "w") as f:
        f.write(value)
    ENV["cookie"] = value
    logging.info(f"Saved cookie to {cf}")


# ---------------------------------------------------------------------------
# Portal interaction
# ---------------------------------------------------------------------------

def make_session():
    s = requests.Session()
    s.headers.update({
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Encoding": "gzip, deflate, br",
    })
    return s


def authenticate(session):
    logging.info("Fetching initial session cookie...")
    r = session.get("https://www.pj-portal.de/")
    init = session.cookies.get_dict().get("PHPSESSID")
    if init:
        save_cookie(init)

    logging.info("Logging in...")
    session.headers.update({
        "Origin": "https://www.pj-portal.de",
        "Referer": "https://www.pj-portal.de/index_uu.php",
    })
    r = session.post("https://www.pj-portal.de/index_uu.php", data={
        "name_Login": "Login",
        "USER_NAME": ENV["pjportal_user"],
        "PASSWORT": ENV["pjportal_pwd"],
        "form_login_submit": "anmelden",
    })
    auth_cookie = session.cookies.get_dict().get("PHPSESSID")
    if auth_cookie:
        save_cookie(auth_cookie)
        logging.info("Login OK, new cookie saved")
    else:
        logging.warning("Login response had no PHPSESSID cookie — credentials may be wrong")


def fetch_merkliste(session):
    logging.info("Fetching Merkliste...")
    cookie = ENV.get("cookie", "")
    if cookie:
        session.cookies.set("PHPSESSID", cookie)
        logging.info(f"Using cookie: {cookie[:6]}…")
    else:
        logging.info("No cookie set")

    session.headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Language": "de-DE,de;q=0.9",
        "Origin": "https://www.pj-portal.de",
        "Referer": "https://www.pj-portal.de/index_uu.php?PAGE_ID=101",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
    })
    r = session.post("https://www.pj-portal.de/ajax.php", data={
        "AJAX_ID": ENV["ajax_uid"],
        "TAB_ID": "Tab_Merkliste",
    })
    logging.info(f"AJAX status={r.status_code} bytes={len(r.content)}")
    logging.info(f"AJAX body preview: {r.content[:300].decode('utf-8', errors='replace')!r}")

    bad = '{"HTML":" Antwort kein Handler ","ERRORCLASS":2}'
    if r.status_code != 200 or r.text == bad:
        raise Exception(f"AJAX rejected: status={r.status_code} body={r.text[:200]!r}")
    return r


def parse_merkliste(response):
    try:
        body = response.json()
    except Exception as e:
        raise Exception(f"Response is not JSON: {e}. Body: {response.text[:300]!r}")

    html_str = body.get("HTML", "")
    if not html_str:
        raise Exception(f"JSON has no HTML key. Keys={list(body.keys())} Body={response.text[:300]!r}")

    logging.info(f"HTML payload: {len(html_str)} chars")
    tree = html.fromstring(html_str)
    rows = tree.xpath("/html/body/table/tr")
    logging.info(f"Parsed {len(rows)} <tr> rows")

    result = {}
    current_tag = ""
    for row in rows:
        cls = row.attrib.get("class", "")

        if cls == "merkliste pj_info_fach":
            for td in row.xpath(".//td"):
                if td.attrib.get("class") == " ":
                    texts = td.xpath(".//text()")
                    if texts:
                        current_tag = texts[0].strip()
                        result[current_tag] = {}

        elif cls == "merkliste_krankenhaus" and current_tag:
            hospital_name = ""
            term_idx = 0
            terms = ["first_term", "second_term", "third_term"]
            slot_classes = {
                "tertial_verfuegbarkeit_beendet  ",
                " tertial_verfuegbarkeit   verfuegbar  buchungsphase  ",
                " tertial_verfuegbarkeit   ausgebucht  buchungsphase  ",
                " tertial_verfuegbarkeit verfuegbar  buchungsphase  ",
                " tertial_verfuegbarkeit ausgebucht  buchungsphase  ",
                " tertial_verfuegbarkeit verfuegbar  ",
                " tertial_verfuegbarkeit ausgebucht  ",
            }
            for td in row.xpath(".//td"):
                td_cls = td.attrib.get("class", "")
                if td_cls == "pj_info_bezeichnung_krankenhaus ":
                    texts = td.xpath(".//text()")
                    if len(texts) >= 3:
                        hospital_name = texts[2].strip()
                        result[current_tag][hospital_name] = {t: None for t in terms}
                elif td_cls in slot_classes and hospital_name and term_idx < 3:
                    texts = td.xpath(".//text()")
                    raw = texts[0].strip() if texts else ""
                    if not raw or raw == "Tertial beendet":
                        raw = "0/0"
                    try:
                        result[current_tag][hospital_name][terms[term_idx]] = tuple(map(int, raw.split("/")))
                    except Exception:
                        result[current_tag][hospital_name][terms[term_idx]] = (0, 0)
                    term_idx += 1

    if not result:
        logging.warning("Parsed result is EMPTY — session is probably not authenticated")
    else:
        logging.info(f"Found {len(result)} specialty groups: {list(result.keys())}")
    return result


def check_slots(merkliste):
    tags      = [x.strip() for x in ENV["pj_tag"].split(",")   if x.strip()]
    hospitals = [x.strip() for x in ENV["hospital"].split(",") if x.strip()]
    terms     = [x.strip() for x in ENV["term"].split(",")     if x.strip()]
    logging.info(f"Checking {len(tags)} tag(s) × {len(hospitals)} hospital(s) × {len(terms)} term(s)")
    for tag in tags:
        for hosp in hospitals:
            for term in terms:
                try:
                    slots = merkliste[tag][hosp][term]
                except KeyError as k:
                    logging.info(f"  {tag} / {hosp} / {term}: not in Merkliste (missing key {k})")
                    continue
                if slots is None:
                    logging.info(f"  {tag} / {hosp} / {term}: not in booking phase")
                    continue
                free, total = slots
                logging.info(f"  {tag} / {hosp} / {term}: {free}/{total}")
                if free > 0:
                    msg = f"SLOT AVAILABLE: {tag} at {hosp} ({term}) — {free}/{total} free!"
                    logging.info(f"  *** {msg}")
                    notify(msg)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run():
    session = make_session()
    try:
        r = fetch_merkliste(session)
        merkliste = parse_merkliste(r)
        if not merkliste:
            raise Exception("empty merkliste on first attempt")
        check_slots(merkliste)
    except Exception as e:
        logging.warning(f"First attempt failed: {e} — re-authenticating")
        session = make_session()
        authenticate(session)
        r = fetch_merkliste(session)
        merkliste = parse_merkliste(r)
        if not merkliste:
            raise Exception("merkliste still empty after re-auth — check credentials or ajax_uid")
        check_slots(merkliste)


if __name__ == "__main__":
    load_env_file()
    logging.info("=" * 50)

    if "--test-notify" in sys.argv:
        logging.info("TEST-NOTIFY MODE")
        load_config()
        notify("PJ-Portal bot test — notification pipeline works!")
        sys.exit(0)

    load_config()
    logging.info("Starting check")
    try:
        run()
        logging.info("Check complete")
    except Exception as e:
        logging.error(f"Check failed: {e}", exc_info=True)
        sys.exit(1)
