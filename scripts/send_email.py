#!/usr/bin/env python3
"""
Global Signal — daily email sender.

Runs right after generate_newsletter.py. Reads the day file that script just
wrote, and if there's anything worth sending, emails it out via Brevo's free
transactional email API to:
  1. A fixed default address (always included, hardcoded below)
  2. Every contact currently in the Brevo subscriber list

If today had zero stories, no email is sent (most newsletters skip "nothing
happened today" emails) - this is logged, not treated as an error.

Requires two secrets, set as environment variables:
  BREVO_API_KEY   - from Brevo dashboard > SMTP & API > API Keys
  BREVO_LIST_ID   - the numeric ID of your subscriber list in Brevo
                    (Contacts > Lists > click your list > ID is in the URL)

Usage:
  python send_email.py            normal run - sends if today has stories
  python send_email.py --force    sends even on a quiet day (for testing)
  python send_email.py --dry-run  renders the email and prints info, but
                                   never actually calls the Brevo API
"""

import argparse
import json
import os
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests
from jinja2 import Environment, FileSystemLoader

# This address always gets the email, regardless of the Brevo subscriber list.
DEFAULT_RECIPIENT = "m.s.haashirshah@gmail.com"

# Brevo requires the "from" address to be a verified sender in your account
# (Brevo dashboard > Senders, Domains & Dedicated IPs > Senders > Add a sender).
# Verifying your own Gmail address is the fastest path - no custom domain needed.
SENDER_EMAIL = os.environ.get("BREVO_SENDER_EMAIL", DEFAULT_RECIPIENT)
SENDER_NAME = "Global Signal"

SITE_URL = "https://haashir12.github.io/News-Letter/"

BREVO_SEND_URL = "https://api.brevo.com/v3/smtp/email"
BREVO_LIST_CONTACTS_URL = "https://api.brevo.com/v3/contacts/lists/{list_id}/contacts"

ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT / "data"
TEMPLATES_DIR = ROOT / "templates"


def find_todays_file():
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    year_dir = DATA_DIR / str(datetime.now(timezone.utc).year)
    path = year_dir / f"{today_str}.json"
    if not path.exists():
        return None
    return path


def get_subscriber_emails(api_key, list_id):
    """Fetches every contact's email from the given Brevo list. Paginates
    since Brevo returns at most 50 contacts per call by default."""
    emails = []
    offset = 0
    limit = 50
    headers = {"api-key": api_key, "accept": "application/json"}
    while True:
        url = BREVO_LIST_CONTACTS_URL.format(list_id=list_id)
        resp = requests.get(url, headers=headers, params={"limit": limit, "offset": offset}, timeout=20)
        if resp.status_code != 200:
            print(f"[warn] could not fetch subscriber list (HTTP {resp.status_code}): {resp.text[:300]}")
            break
        payload = resp.json()
        contacts = payload.get("contacts", [])
        emails.extend(c["email"] for c in contacts if c.get("email"))
        if len(contacts) < limit:
            break
        offset += limit
    return emails


def send_email(api_key, recipients, subject, html_content):
    headers = {
        "api-key": api_key,
        "content-type": "application/json",
        "accept": "application/json",
    }
    body = {
        "sender": {"name": SENDER_NAME, "email": SENDER_EMAIL},
        "to": [{"email": e} for e in recipients],
        "subject": subject,
        "htmlContent": html_content,
    }
    resp = requests.post(BREVO_SEND_URL, headers=headers, json=body, timeout=30)
    if resp.status_code >= 300:
        print(f"[error] Brevo send failed (HTTP {resp.status_code}): {resp.text[:500]}")
        return False
    print(f"[info] email sent to {len(recipients)} recipient(s)")
    return True


def format_date_label(iso_ts):
    dt = datetime.fromisoformat(iso_ts)
    return dt.strftime("%B %-d, %Y") if os.name != "nt" else dt.strftime("%B %d, %Y")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--force", action="store_true", help="Send even if today has zero stories.")
    parser.add_argument("--dry-run", action="store_true", help="Render the email and print info, but don't call Brevo.")
    args = parser.parse_args()

    path = find_todays_file()
    if not path:
        print("[error] no data file found for today - run generate_newsletter.py first.")
        sys.exit(1)

    day_data = json.loads(path.read_text())

    if day_data["total_stories"] == 0 and not args.force:
        print("[info] no stories today - skipping email (use --force to send anyway).")
        return

    api_key = os.environ.get("BREVO_API_KEY")
    list_id = os.environ.get("BREVO_LIST_ID")
    if not args.dry_run and not api_key:
        print("[error] BREVO_API_KEY environment variable is not set.")
        sys.exit(1)

    env = Environment(loader=FileSystemLoader(str(TEMPLATES_DIR)), autoescape=False)
    tpl = env.get_template("email.html")
    date_label = format_date_label(day_data["run_timestamp"])
    html = tpl.render(
        date_label=date_label,
        countries=day_data["countries"],
        site_url=SITE_URL,
        site_url_display=SITE_URL.replace("https://", "").rstrip("/"),
    )

    recipients = [DEFAULT_RECIPIENT]
    if list_id and api_key:
        subscribers = get_subscriber_emails(api_key, list_id)
        for e in subscribers:
            if e not in recipients:
                recipients.append(e)
    print(f"[info] recipients for today: {len(recipients)} (including default)")

    subject = f"Global Signal — {date_label} — {day_data['total_stories']} stories"

    if args.dry_run:
        print("[dry-run] would send to:", recipients)
        print("[dry-run] subject:", subject)
        out_path = ROOT / "email_preview.html"
        out_path.write_text(html)
        print(f"[dry-run] rendered email saved to {out_path}")
        return

    send_email(api_key, recipients, subject, html)


if __name__ == "__main__":
    main()
