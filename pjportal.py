import sys
import os
import logging
import requests
from lxml import html




logging.basicConfig(
    format="%(asctime)s - %(levelname)s - %(message)s",
    level=logging.INFO,
    datefmt="%Y-%m-%d %H:%M:%S%z"
)

ENV_VAR_OPTIONAL = ['pushover_user', 'pushover_token', 'ntfy_url_topic', 'cookie_filepath', 'cookie_default_value']
ENV_VAR_REQUIRED = ['pjportal_user', 'pjportal_pwd', 'ajax_uid', 'pj_tag', 'hospital', 'term']
ENV_VAR_list = ENV_VAR_REQUIRED + ENV_VAR_OPTIONAL
ENV_VAR = {}


def read_secret(name):
    """Read a config value: prefer systemd-provided credential file, fall back to env var.

    When systemd runs the unit with LoadCredentialEncrypted=, the decrypted value lands
    at $CREDENTIALS_DIRECTORY/<name> on a tmpfs. For local dev without systemd,
    plain env vars still work.
    """
    creds_dir = os.environ.get("CREDENTIALS_DIRECTORY")
    if creds_dir:
        path = os.path.join(creds_dir, name)
        if os.path.exists(path):
            with open(path) as f:
                return f.read().strip()
    return os.environ.get(name)


def _mask(s):
    """Return a short, log-safe fingerprint of a secret (first 4 + last 2 chars)."""
    if not s:
        return "<empty>"
    if len(s) <= 6:
        return f"<len={len(s)}>"
    return f"{s[:4]}…{s[-2:]} (len={len(s)})"


def load_env_file(path="/etc/pjportal.env"):
    """Populate os.environ from the shared env file for keys not already set.

    Lets the script run either under systemd (which already provides these via
    EnvironmentFile=) or manually from a shell (where the caller hasn't sourced
    /etc/pjportal.env). Systemd's values always take precedence because we skip
    keys already present in the environment.
    """
    if not path or not os.path.exists(path):
        return
    try:
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, _, val = line.partition("=")
                key = key.strip()
                if key and key not in os.environ:
                    os.environ[key] = val.strip()
    except OSError as e:
        logging.debug(f"Could not read env file {path}: {e}")


def load_env(require_pjportal=True):
    global ENV_VAR
    ENV_VAR = {var_name: read_secret(var_name) for var_name in ENV_VAR_list}
    if require_pjportal:
        missing_vars = [key for key, value in ENV_VAR.items() if key not in ENV_VAR_OPTIONAL and value is None]
        if missing_vars:
            raise ValueError(f"Error: Missing required environment variables: {', '.join(missing_vars)}")
    logging.debug(f"pjportal_user={ENV_VAR['pjportal_user']!r} ajax_uid={ENV_VAR['ajax_uid']!r}")
    logging.debug(f"pj_tag={ENV_VAR['pj_tag']!r} hospital={ENV_VAR['hospital']!r} term={ENV_VAR['term']!r}")
    if ENV_VAR['cookie_filepath'] and os.path.exists(ENV_VAR['cookie_filepath']):
        with open(ENV_VAR['cookie_filepath'], "r") as file:
            ENV_VAR['cookie_default_value'] = file.read().strip()
            logging.info(f"Loaded cookie from {ENV_VAR['cookie_filepath']}: {_mask(ENV_VAR['cookie_default_value'])}")
    elif ENV_VAR['cookie_default_value']:
        logging.info(f"No cookie file at {ENV_VAR['cookie_filepath']}, seeding from cookie_default_value")
        save_cookie(ENV_VAR['cookie_default_value'])
    else:
        logging.info(f"No cookie available yet (cookie_filepath={ENV_VAR['cookie_filepath']!r}); will authenticate on first request")
    logging.info("Successfully loaded all required environment variables.")
    return ENV_VAR



def save_cookie(cookie_value):
    if not ENV_VAR.get('cookie_filepath'):
        return
    with open(ENV_VAR['cookie_filepath'], "w") as file:
        file.write(cookie_value)
    logging.info(f"Saved cookie to {ENV_VAR['cookie_filepath']}")



def get_init_session_cookie(session):
    logging.info("Accessing site...")
    response = session.get(url="https://www.pj-portal.de/")
    init_cookie = session.cookies.get_dict().get("PHPSESSID")
    if init_cookie:
        save_cookie(init_cookie)
    return session



def get_auth_session_cookie(session):
    logging.info("Starting authentication...")
    session.headers.update({
        "Origin": "https://www.pj-portal.de",
        "Referer": "https://www.pj-portal.de/index_uu.php",
    })
    data = {
        "name_Login": "Login",
        "USER_NAME": ENV_VAR["pjportal_user"],
        "PASSWORT": ENV_VAR["pjportal_pwd"],
        "form_login_submit": "anmelden"
    }
    url = "https://www.pj-portal.de/index_uu.php"
    response = session.post(url, data=data)
    new_cookie = session.cookies.get_dict().get("PHPSESSID")
    if new_cookie:
        save_cookie(new_cookie)
    logging.info("Authentication successfully completed...")
    return session



def request_open_slots(session, cookie=None):
    logging.info("Grabing data...")
    session.headers.update({
        "Accept": "application/json, text/javascript, */*; q=0.01",
        "Accept-Encoding": "gzip, deflate, br, zstd",
        "Accept-Language": "de-DE,de;q=0.9,en-US;q=0.8,en;q=0.7",
        "Origin": "https://www.pj-portal.de",
        "Referer": "https://www.pj-portal.de/index_uu.php?PAGE_ID=101",
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8"
    })
    data = {"AJAX_ID": ENV_VAR["ajax_uid"], "TAB_ID": "Tab_Merkliste"}
    if cookie:
        logging.info(f"Using preset cookie {_mask(cookie)}")
        session.cookies.set("PHPSESSID", cookie)
    else:
        logging.info("No preset cookie — request will rely on session cookies from login")
    response = session.post("https://www.pj-portal.de/ajax.php", data=data)
    body_preview = response.content[:300].decode("utf-8", errors="replace")
    logging.info(f"AJAX response: status={response.status_code}, bytes={len(response.content)}, preview={body_preview!r}")
    if response.status_code == 200 and response.content.decode('utf-8') != '{"HTML":" Antwort kein Handler ","ERRORCLASS":2}':
        return response
    logging.warning(f"Request failed: status={response.status_code}, body={response.content[:500]!r}")
    raise Exception("AJAX request rejected — likely stale cookie")



def extract_table_from_response(response):

    parsing_result_dict = {}

    try:
        jsonobj = response.json()
    except Exception as e:
        logging.warning(f"AJAX body is not JSON: {e}. First 500 bytes: {response.content[:500]!r}")
        return parsing_result_dict
    htmltable = jsonobj.get("HTML")
    if not htmltable:
        logging.warning(f"AJAX JSON has no HTML field. Keys: {list(jsonobj.keys())}. Full body: {response.content[:500]!r}")
        return parsing_result_dict
    logging.info(f"HTML payload: {len(htmltable)} chars, preview={htmltable[:200]!r}")
    tree = html.fromstring(htmltable)
    main_xpath = f"/html/body/table/tr"

    all_rows = tree.xpath(main_xpath)
    class_counts = {}
    for row in all_rows:
        cls = row.attrib.get("class", "<no class>")
        class_counts[cls] = class_counts.get(cls, 0) + 1
    logging.info(f"Parsed {len(all_rows)} <tr> rows. Class breakdown: {class_counts}")

    i = 0
    pj_tag = ""
    for row in all_rows:

        i+=1
        if row.attrib["class"] == "merkliste pj_info_fach":
            cols = row.xpath('.//td')
            for elem in cols:
                if (elem.attrib["class"]) == ' ':
                    pj_tag = elem.xpath('.//text()')[0].strip()
                    parsing_result_dict[pj_tag] = {}

        elif row.attrib["class"] == "merkliste_krankenhaus":
            cols = row.xpath('.//td')
            tertiar_counter = 0
            term_desc = ["first_term", "second_term", "third_term"]
            for elem in cols:
                if 'class' in elem.attrib:

                    if (elem.attrib["class"]) == "pj_info_bezeichnung_krankenhaus ":

                        hospital = elem.xpath('.//text()')[2].strip()
                        parsing_result_dict[pj_tag][hospital] = {term_desc[0]: None, term_desc[1]: None, term_desc[2]: None}
     
                    if (elem.attrib["class"]) in ["tertial_verfuegbarkeit_beendet  ", " tertial_verfuegbarkeit   verfuegbar  buchungsphase  ", " tertial_verfuegbarkeit   ausgebucht  buchungsphase  ", " tertial_verfuegbarkeit verfuegbar  buchungsphase  ", " tertial_verfuegbarkeit ausgebucht  buchungsphase  ", " tertial_verfuegbarkeit verfuegbar  ", " tertial_verfuegbarkeit ausgebucht  "]:
                        testint = elem.xpath('.//text()')
                        try:
                            slots = elem.xpath('.//text()')[0].strip()
                        except:
                            slots = '0/0'
                        slots = slots or '0/0'
                        if slots == 'Tertial beendet':
                            slots = '0/0'
                        parsing_result_dict[pj_tag][hospital][term_desc[tertiar_counter]] = tuple(map(int, slots.split('/')))
                        tertiar_counter += 1

    if not parsing_result_dict:
        logging.warning("Parsed table is EMPTY — no pj_info_fach rows matched. Session likely unauthenticated.")
    else:
        logging.info(f"Parsed {len(parsing_result_dict)} specialty groups: {list(parsing_result_dict.keys())}")
    return parsing_result_dict


def send_push_message(msg):
    pushover_user = ENV_VAR.get('pushover_user')
    pushover_token = ENV_VAR.get('pushover_token')
    ntfy_url_topic = ENV_VAR.get('ntfy_url_topic')

    channels = []
    if pushover_user and pushover_token:
        channels.append("pushover")
    if ntfy_url_topic:
        channels.append("ntfy")
    if not channels:
        logging.error("NO NOTIFICATION CHANNELS CONFIGURED. Set pushover_user+pushover_token or ntfy_url_topic in /etc/pjportal.env.")
        return

    logging.info(f"Dispatching push via {', '.join(channels)}: {msg!r}")
    if "pushover" in channels:
        send_pushover_notification(msg)
    if "ntfy" in channels:
        send_ntfy_notification(msg)


def send_pushover_notification(msg):
    try:
        resp = requests.post(
            "https://api.pushover.net/1/messages.json",
            data={
                "token": ENV_VAR['pushover_token'],
                "user": ENV_VAR['pushover_user'],
                "message": msg,
            },
            timeout=10,
        )
    except Exception as e:
        logging.error(f"Pushover request failed to send: {e}")
        return
    if resp.status_code == 200:
        logging.info(f"Pushover: sent OK ({resp.status_code})")
    else:
        logging.error(f"Pushover: FAILED status={resp.status_code} body={resp.text!r}")


def send_ntfy_notification(msg):
    try:
        resp = requests.post(
            ENV_VAR["ntfy_url_topic"],
            data=msg.encode('utf-8'),
            headers={"Title": "Found something on pj-portal.de", "Priority": "5"},
            timeout=10,
        )
    except Exception as e:
        logging.error(f"ntfy request failed to send: {e}")
        return
    if resp.status_code == 200:
        logging.info(f"ntfy: sent OK ({resp.status_code})")
    else:
        logging.error(f"ntfy: FAILED status={resp.status_code} body={resp.text!r}")


def run_main():

    session = requests.Session()
    session.headers.update({
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/132.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8,application/signed-exchange;v=b3;q=0.7",
            "Accept-Encoding": "gzip, deflate, br, zstd",
        })

    def run_auth(session):
        session = get_init_session_cookie(session)
        session = get_auth_session_cookie(session)
        return session

    def check_one(table_dict, pj_tag, hospital, term):
        try:
            result_tuple = table_dict[pj_tag][hospital][term]
        except KeyError as missing:
            logging.warning(f"Skipping {pj_tag} / {hospital} / {term}: key {missing} not in Merkliste")
            return
        if result_tuple is None:
            logging.info(f"Not in booking phase: {pj_tag} / {hospital} / {term}")
            return
        info = f"{result_tuple[0]}/{result_tuple[1]}"
        if result_tuple[0] > 0:
            msg = f"Found something for {pj_tag}, {hospital}, {term}! {info}!"
            logging.info(msg)
            send_push_message(msg=msg)
        else:
            logging.info(f"Nothing found for {pj_tag}, {hospital}, {term}: {info}")

    def run_table_check(table_dict, pj_tag, hospital, term):
        logging.info("Parsing data from request and checking the table...")
        logging.info(table_dict)
        tags       = [x.strip() for x in pj_tag.split(",")   if x.strip()]
        hospitals  = [x.strip() for x in hospital.split(",") if x.strip()]
        terms      = [x.strip() for x in term.split(",")     if x.strip()]
        for t in tags:
            for h in hospitals:
                for tm in terms:
                    check_one(table_dict, t, h, tm)


    def do_check():
        response = request_open_slots(session, cookie=ENV_VAR['cookie_default_value'])
        table_dict = extract_table_from_response(response)
        # A logged-in user with a non-empty Merkliste will always have at least one specialty.
        # An empty dict means the cookie was accepted at the HTTP layer but the session isn't
        # actually authenticated — fall through to reauth.
        if not table_dict:
            raise Exception("empty merkliste — session not truly authenticated")
        run_table_check(table_dict=table_dict, pj_tag=ENV_VAR["pj_tag"], hospital=ENV_VAR["hospital"], term=ENV_VAR["term"])

    try:
        do_check()

    except IndexError:
        logging.warning("IndexError while parsing response — likely stale cookie or portal layout change.")
        raise

    except Exception as e:
        logging.warning(f"First attempt failed ({e}); clearing cookies and re-authenticating.")
        session.cookies.clear()
        # Wipe the on-disk cookie so we don't reuse the bad one next tick
        if ENV_VAR.get('cookie_filepath') and os.path.exists(ENV_VAR['cookie_filepath']):
            os.remove(ENV_VAR['cookie_filepath'])
            logging.info("Removed stale cookie file")
        ENV_VAR['cookie_default_value'] = None
        session = run_auth(session)
        response = request_open_slots(session)
        table_dict = extract_table_from_response(response)
        if not table_dict:
            raise Exception("re-authenticated but merkliste is still empty — check pjportal_user/pjportal_pwd or ajax_uid")
        run_table_check(table_dict=table_dict, pj_tag=ENV_VAR["pj_tag"], hospital=ENV_VAR["hospital"], term=ENV_VAR["term"])

    logging.info("Script completed.")


if __name__ == "__main__":
    load_env_file()  # no-op under systemd; picks up /etc/pjportal.env for manual runs
    logging.info("--------------------------------------------")
    logging.info("Script started")

    if "--test-notify" in sys.argv:
        load_env(require_pjportal=False)
        logging.info("Test-notify mode: sending a canary push and exiting.")
        send_push_message("PJ-Portal bot test notification — if you see this, the pipeline works.")
        sys.exit(0)

    load_env()
    try:
        run_main()
    except Exception as e:
        logging.error(f"Check failed: {e}", exc_info=True)
        sys.exit(1)