# PJ-Portal Bot for Checking Available Slots
## An open-source, data-privacy-oriented alternative to quickpj.de
Many medical students face challenges securing a spot at their preferred hospital for their PJ. While [www.quickpj.de](https://www.quickpj.de/) provides a convenient way to receive notifications when desired slots become available on pj-portal.de, its approach raises privacy concerns. The platform requires full login credentials, which, if compromised, could allow third parties to deregister students from their entire practical training. Additionally, quickpj.de is a paid service, adding a financial barrier for students.

This fork runs the bot as a **single Python script scheduled by systemd**, deployed to a free Ubuntu VM (Oracle Cloud Always Free is the recommended host). No Docker, no external services, no monthly cost.

## Prerequisites
Your university must be among the participating universities of pj-portal.de and you must already have obtained login credentials for the platform. A list of the participating universities can be found on pj-portal.de. Those include currently:
Universität Münster, RWTH Aachen, Universität Augsburg, Charité Berlin, MSB Berlin, Ruhr Universität Bochum, Universität Bonn, Medizinische Hochschule Brandenburg, Technische Universität Dresden, Heinrich-Heine-Universität Düsseldorf, FAU Erlangen-Nürnberg, Goethe Universität Frankfurt am Main, Justus-Liebig-Universität Giessen, Georg-August-Universität Göttingen, Universität Greifswald, Martin-Luther-Universität Halle-Wittenberg, MSH Medical School Hamburg, Universität Hamburg, Medizinische Hochschule Hannover, Universität Heidelberg, Friedrich-Schiller-Universität Jena, Christian-Albrechts-Universität zu Kiel, Universität Leipzig, Universität zu Lübeck, Otto-von-Guericke-Universität Magdeburg, Johannes Gutenberg-Universität Mainz, Philipps-Universität Marburg, LMU München, TU München, Carl von Ossietzky Universität Oldenburg, Health and Medical University Potsdam, Universität Rostock, Universität des Saarlandes, Eberhard Karls Universität Tübingen, Universität Ulm, Universität Witten/Herdecke, Julius-Maximilians-Universität Würzburg.

## How it works
- `pjportal.py` runs **once per invocation**: checks the merkliste for the configured PJ tag / hospital / term and pushes a notification if a slot is open. Then it exits.
- A **systemd timer** re-fires the script every 60–360 seconds (matches the original random interval).
- The `PHPSESSID` cookie is persisted to `/var/lib/pjportal/cookie.txt` so most runs skip the login round-trip.
- Your **pj-portal.de password never touches the filesystem in plaintext**. It is sealed to the machine with `systemd-creds` and only decrypted into a tmpfs while the service is running. A stolen disk snapshot cannot be decrypted on another host.

## 1. Notification setup
Pick at least one channel — the bot supports [Pushover](https://pushover.net) and [ntfy](https://ntfy.sh).

**Pushover**
1. Sign up at [pushover.net/signup](https://pushover.net/signup) (30-day free trial, then ~$5 one-time).
2. Note your **user key**.
3. Install the Pushover app on your phone and log in.
4. Create an application at [pushover.net/apps/build](https://pushover.net/apps/build) and note the **API token**.

**ntfy** (free)
1. Pick a hard-to-guess topic name, e.g. `pjportal-x9k4pQ`.
2. Install the ntfy app and subscribe to `https://ntfy.sh/pjportal-x9k4pQ`.
3. Use that full URL as `ntfy_url_topic`.

## 2. Get your `ajax_uid`
1. Log in to pj-portal.de.
2. Open the "PJ Angebot" tab.
3. Open browser DevTools → **Network** tab. Clear the log.
4. Click the big **Merkliste aktualisieren** refresh button on the page (do NOT reload the browser).
5. Click the `ajax.php` request → **Payload** tab.
6. Copy the `AJAX_ID` value (7-digit integer).

## 3. Create an Oracle Cloud Always Free VM
1. Sign up at [oracle.com/cloud/free](https://www.oracle.com/cloud/free/). Credit card required for verification but never charged for Always Free resources.
2. Console → **Compute → Instances → Create instance**.
3. Image: **Ubuntu 24.04** (required — 22.04's systemd is too old for encrypted credentials). Shape: **VM.Standard.A1.Flex** (ARM) — assign 1 OCPU / 6 GB RAM. Plenty for this workload and leaves room for future projects.
4. Add your SSH public key.
5. Leave networking defaults — this workload only makes outbound calls, no inbound ports needed.
6. Wait ~1 min for the VM to come up, then SSH in: `ssh ubuntu@<public-ip>`.

## 4. Install the bot
On the VM:

```bash
# One-shot: clones the repo, builds venv, seeds env file, prints next step
curl -fsSL https://raw.githubusercontent.com/<you>/<repo>/main/deploy/install.sh | sudo REPO=https://github.com/<you>/<repo>.git bash
```

The installer will stop and ask you to fill in credentials:

```bash
sudo nano /etc/pjportal.env
```

Fill in the env file (see `deploy/pjportal.env.example` for the full schema), then re-run the installer to enable the timer:

```bash
sudo bash /opt/pjportal/deploy/install.sh
```

That's it. The timer is now running.

### Environment variables

| Variable | Required | Example | Description |
|---|---|---|---|
| `pjportal_user` | yes | `max.mustermann@uni-muster.de` | Login email for pj-portal.de |
| `pjportal_pwd` | yes | `super-secure-password1` | Password for pj-portal.de |
| `ajax_uid` | yes | `5102130` | See step 2 |
| `pj_tag` | yes | `Allgemeinmedizin` | Specialty exactly as it appears in "PJ Angebot" → "Krankenhäuser" (`Chirurgie`, `Innere Medizin`, `Anästhesiologie`, …) |
| `hospital` | yes | `Ulm Universitätsklinikum` | Hospital name exactly as it appears in "Krankenhäuser" (`Berlin Charité`, `Hamburg Univ.`, …) |
| `term` | yes | `second_term` | One of `first_term`, `second_term`, `third_term` |
| `cookie_filepath` | no | `/var/lib/pjportal/cookie.txt` | Persistent cookie path. Default is set in `pjportal.env.example` |
| `cookie_default_value` | no | `901p3g53lo041j4pcl5po5xcws` | Seed cookie used only when no cookie file exists |
| `pushover_user` | no | `xg7m2vtqnflo5p3zbkydw64cjj8r9s` | Pushover user key |
| `pushover_token` | no | `a4ot22m6d3569wwk76wpgsc3jdyfv4` | Pushover application token |
| `ntfy_url_topic` | no | `https://ntfy.sh/pjportal-x9k4pQ` | Full ntfy topic URL |

## 5. Operations

```bash
# Is the timer scheduled?
systemctl list-timers pjportal.timer

# Follow logs in real time
journalctl -u pjportal -f

# Force one check right now (great for verifying config)
sudo systemctl start pjportal.service

# Stop / restart the schedule
sudo systemctl disable --now pjportal.timer
sudo systemctl enable --now pjportal.timer

# Update to latest main
sudo bash /opt/pjportal/deploy/install.sh
```

### Testing notifications
Set `pj_tag`, `hospital`, and `term` to a combination that you know currently has open slots. Then `sudo systemctl start pjportal.service`. You should get a push within a couple of seconds.

## 6. Secret handling
`install.sh` encrypts your pj-portal.de password with `systemd-creds` (host-key-sealed) and writes the ciphertext to `/etc/pjportal/pjportal_pwd.cred`. The plaintext never lands on disk. At service start systemd decrypts it into `/run/credentials/pjportal.service/pjportal_pwd`, which is a per-service tmpfs — gone when the unit stops.

### Rotate the password
```bash
sudo rm /etc/pjportal/pjportal_pwd.cred
sudo bash /opt/pjportal/deploy/install.sh   # prompts for the new one
```

### Encrypt other secrets (pushover_token, ntfy_url_topic, ...)
```bash
# 1. Encrypt
sudo mkdir -p /etc/pjportal
echo -n 'YOUR_PUSHOVER_TOKEN' | sudo systemd-creds encrypt --name=pushover_token - /etc/pjportal/pushover_token.cred
sudo chmod 600 /etc/pjportal/pushover_token.cred

# 2. Reference it from the service unit
sudo systemctl edit pjportal.service
# In the drop-in editor, add:
#   [Service]
#   LoadCredentialEncrypted=pushover_token:/etc/pjportal/pushover_token.cred

# 3. Remove the plaintext value from /etc/pjportal.env
sudo nano /etc/pjportal.env

sudo systemctl daemon-reload
```

Any variable named the same as the credential (`pushover_token` here) will be picked up automatically — the Python code reads `$CREDENTIALS_DIRECTORY/<name>` before falling back to env.

### Threat model
| Attacker | Coverage |
|---|---|
| Non-root user on the VM reading `/etc/pjportal/*.cred` | Encrypted, unreadable |
| Stolen disk image / snapshot mounted on another host | Sealed to this machine — unrecoverable |
| Root on the running VM | Can read `/run/credentials/pjportal.service/*` while the service runs — inherent, no software fix |
| Compromise of your Oracle account | Full VM access. Enable Oracle MFA. |

## Local development
```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
set -a; source ./deploy/pjportal.env.example; set +a   # then edit the exports
python pjportal.py
```

## Contribution
PRs welcome.

## Credits
Forked from [madrhr/pj-portal-bot](https://github.com/madrhr/pj-portal-bot). This fork drops the Docker layer, converts the poller to one-shot mode, and switches scheduling to systemd.
