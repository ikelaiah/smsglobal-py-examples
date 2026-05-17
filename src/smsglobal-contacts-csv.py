"""Push a CSV file of contacts into a single SMSGlobal group.

Usage:
    python src/smsglobal-contacts-csv.py <csv_file> [--mode add|upsert|sync] [--yes]

Modes:
  add    - only POST new contacts (existing contacts untouched)
  upsert - add new + update changed contacts (default; no deletes)
  sync   - full set-diff including DELETE of contacts not in the CSV

CSV headers (case-insensitive):
    FIRSTNAME, SURNAME, DISPLAY_NAME, MOBILE_PHONE, EMAIL_ADDRESS

Only MOBILE_PHONE is required per row; rows without a usable phone are
skipped.
"""

import os
import sys
import re
import csv
import argparse
import time
import hmac
import hashlib
import base64
import secrets
from datetime import datetime
from urllib.parse import urlsplit

import requests
from dotenv import load_dotenv


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# SMSGlobal Target Group (name as it appears in SMSGlobal). Created if missing.
smsglobal_group = "SMSGLOBAL_GROUP"

# CSV column -> SMSGlobal field
CSV_columns_to_match = {
    "FIRSTNAME": "givenName",
    "SURNAME": "familyName",
    "DISPLAY_NAME": "displayName",
    "MOBILE_PHONE": "msisdn",
    "EMAIL_ADDRESS": "emailAddress",
}

COMPARE_FIELDS = ["givenName", "familyName", "displayName", "emailAddress"]

SMSGLOBAL_BASE = "https://api.smsglobal.com/v2"
DEFAULT_TIMEOUT = 30


# ─────────────────────────────────────────────────────────────────────────────
# Phone normalisation
# ─────────────────────────────────────────────────────────────────────────────

def normalise_phone(raw):
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None
    if digits.startswith("00"):
        digits = digits[2:]
        has_plus = True
    if has_plus:
        return digits
    if digits.startswith("61"):
        return digits
    if digits.startswith("0"):
        return "61" + digits[1:]
    if len(digits) == 9 and digits.startswith("4"):
        return "61" + digits
    return digits


# ─────────────────────────────────────────────────────────────────────────────
# CSV reader
# ─────────────────────────────────────────────────────────────────────────────

def read_csv_contacts(path):
    """Read a CSV and return list of contact dicts keyed by SMSGlobal fields.

    Headers are matched case-insensitively. Rows with no usable MOBILE_PHONE
    are skipped silently.
    """
    contacts = []
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        if reader.fieldnames is None:
            raise ValueError(f"CSV has no header row: {path}")

        # Build a case-insensitive lookup: upper(header) -> actual header
        header_lookup = {h.strip().upper(): h for h in reader.fieldnames if h}
        missing = [c for c in CSV_columns_to_match if c not in header_lookup]
        if "MOBILE_PHONE" in missing:
            raise ValueError(
                f"CSV is missing required column MOBILE_PHONE. "
                f"Found: {list(reader.fieldnames)}"
            )
        if missing:
            print(
                f"  ⚠️  CSV missing optional columns: {', '.join(missing)} "
                f"(will be sent as null)"
            )

        def cell(row, csv_col):
            actual = header_lookup.get(csv_col)
            if actual is None:
                return None
            val = row.get(actual)
            if val is None:
                return None
            val = val.strip()
            return val or None

        for row in reader:
            phone = normalise_phone(cell(row, "MOBILE_PHONE"))
            if not phone:
                continue
            contacts.append({
                "msisdn": phone,
                "givenName": cell(row, "FIRSTNAME"),
                "familyName": cell(row, "SURNAME"),
                "displayName": cell(row, "DISPLAY_NAME"),
                "emailAddress": cell(row, "EMAIL_ADDRESS"),
            })
    return contacts


# ─────────────────────────────────────────────────────────────────────────────
# SMSGlobal client (MAC authentication, with 429 backoff)
# ─────────────────────────────────────────────────────────────────────────────

class SMSGlobalClient:
    def __init__(self, key, secret):
        self.key = key
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self.session = requests.Session()

    def _auth_header(self, method, url):
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        uri = parts.path or "/"
        if parts.query:
            uri = f"{uri}?{parts.query}"
        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)
        mac_input = f"{ts}\n{nonce}\n{method}\n{uri}\n{host}\n{port}\n\n"
        digest = hmac.new(self.secret, mac_input.encode("utf-8"), hashlib.sha256).digest()
        mac = base64.b64encode(digest).decode("ascii")
        return f'MAC id="{self.key}", ts="{ts}", nonce="{nonce}", mac="{mac}"'

    def _request(self, method, path, **kwargs):
        url = f"{SMSGLOBAL_BASE}{path}"
        if "params" in kwargs and kwargs["params"]:
            prepared = requests.Request(method, url, params=kwargs.pop("params")).prepare()
            url = prepared.url
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        body_headers = kwargs.pop("headers", {}) or {}

        max_attempts = 5
        backoff = 2.0
        for attempt in range(1, max_attempts + 1):
            headers = dict(body_headers)
            headers["Authorization"] = self._auth_header(method, url)
            resp = self.session.request(method, url, headers=headers, **kwargs)

            if resp.status_code == 429:
                if attempt == max_attempts:
                    raise RuntimeError(
                        f"{method} {path} -> HTTP 429 after {max_attempts} attempts: "
                        f"{resp.text[:300]}"
                    )
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = float(retry_after) if retry_after else backoff
                except ValueError:
                    wait = backoff
                print(f"    ⏳ 429 rate-limited; sleeping {wait:.1f}s (attempt {attempt}/{max_attempts})")
                time.sleep(wait)
                backoff = min(backoff * 2, 30.0)
                continue

            if not resp.ok:
                raise RuntimeError(
                    f"{method} {path} -> HTTP {resp.status_code}: {resp.text[:300]}"
                )
            if resp.status_code == 204 or not resp.content:
                return None
            return resp.json()

    def list_groups(self):
        groups = []
        offset = 0
        limit = 100
        while True:
            data = self._request("GET", f"/group?limit={limit}&offset={offset}")
            page = (data or {}).get("data") or (data or {}).get("groups") or []
            if not page and isinstance(data, list):
                page = data
            groups.extend(page)
            total = (data or {}).get("total")
            if total is None:
                if len(page) < limit:
                    break
            elif offset + len(page) >= total:
                break
            if not page:
                break
            offset += len(page)
        return groups

    def create_group(self, name):
        return self._request("POST", "/group", json={"name": name})

    def list_group_contacts(self, group_id):
        contacts = []
        offset = 0
        limit = 20
        while True:
            data = self._request(
                "GET",
                f"/group/{group_id}/contacts?limit={limit}&offset={offset}",
            )
            page = (data or {}).get("data") or (data or {}).get("contacts") or []
            contacts.extend(page)
            total = (data or {}).get("total")
            if total is None:
                if len(page) < limit:
                    break
            elif offset + len(page) >= total:
                break
            if not page:
                break
            offset += len(page)
        return contacts

    def add_contact(self, group_id, payload):
        return self._request("POST", f"/group/{group_id}/contact", json=payload)

    def update_contact(self, contact_id, payload):
        return self._request("PUT", f"/contact/{contact_id}", json=payload)

    def delete_contact(self, contact_id):
        return self._request("DELETE", f"/contact/{contact_id}")


# ─────────────────────────────────────────────────────────────────────────────
# Sync logic
# ─────────────────────────────────────────────────────────────────────────────

def _build_payload(contact):
    return {k: v for k, v in contact.items() if v is not None}


def _needs_update(remote, local):
    for field in COMPARE_FIELDS:
        if (remote.get(field) or None) != (local.get(field) or None):
            return True
    return False


def sync_group(client, group_name, csv_contacts, mode):
    """mode: "add" | "upsert" | "sync" — see module docstring."""
    stats = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}
    print(f"\n── CSV → {group_name} (mode={mode})")

    try:
        groups = client.list_groups()
    except Exception as e:
        print(f"  ❌ Failed to list groups: {e}")
        stats["errors"] += 1
        return stats

    group = next((g for g in groups if g.get("name") == group_name), None)
    if group is None:
        try:
            print(f"  ➕ Creating group '{group_name}'")
            group = client.create_group(group_name)
        except Exception as e:
            print(f"  ❌ Failed to create group '{group_name}': {e}")
            stats["errors"] += 1
            return stats
    group_id = group.get("id")
    if group_id is None:
        print(f"  ❌ Group '{group_name}' has no id in API response")
        stats["errors"] += 1
        return stats

    try:
        remote_raw = client.list_group_contacts(group_id)
    except Exception as e:
        print(f"  ❌ Failed to list contacts for group {group_id}: {e}")
        stats["errors"] += 1
        return stats

    remote_by_phone = {}
    for c in remote_raw:
        phone = normalise_phone(c.get("msisdn"))
        if not phone:
            continue
        remote_by_phone[phone] = {
            "id": c.get("id"),
            "msisdn": phone,
            "givenName": c.get("givenName"),
            "familyName": c.get("familyName"),
            "displayName": c.get("displayName"),
            "emailAddress": c.get("emailAddress"),
        }

    local_by_phone = {}
    for c in csv_contacts:
        if c["msisdn"] not in local_by_phone:
            local_by_phone[c["msisdn"]] = c
        else:
            stats["skipped"] += 1

    remote_keys = set(remote_by_phone)
    local_keys = set(local_by_phone)
    to_add = local_keys - remote_keys
    to_delete = remote_keys - local_keys
    to_check = remote_keys & local_keys

    print(
        f"  Remote: {len(remote_by_phone)}  CSV: {len(local_by_phone)}  "
        f"+{len(to_add)} ~{len(to_check)} -{len(to_delete)}"
    )

    for phone in to_add:
        try:
            client.add_contact(group_id, _build_payload(local_by_phone[phone]))
            stats["added"] += 1
        except Exception as e:
            print(f"    ❌ add {phone}: {e}")
            stats["errors"] += 1

    if mode in ("upsert", "sync"):
        for phone in to_check:
            remote = remote_by_phone[phone]
            local = local_by_phone[phone]
            if not _needs_update(remote, local):
                continue
            payload = _build_payload({k: local.get(k) for k in COMPARE_FIELDS})
            try:
                client.update_contact(remote["id"], payload)
                stats["updated"] += 1
            except Exception as e:
                print(f"    ❌ update {phone} (id={remote['id']}): {e}")
                stats["errors"] += 1

    if mode == "sync":
        for phone in to_delete:
            contact_id = remote_by_phone[phone]["id"]
            try:
                client.delete_contact(contact_id)
                stats["deleted"] += 1
            except Exception as e:
                print(f"    ❌ delete {phone} (id={contact_id}): {e}")
                stats["errors"] += 1

    print(
        f"  ✅ added={stats['added']} updated={stats['updated']} "
        f"deleted={stats['deleted']} errors={stats['errors']}"
    )
    return stats


# ─────────────────────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────────────────────

def main():
    load_dotenv()

    parser = argparse.ArgumentParser(description="Sync a CSV file of contacts into an SMSGlobal group.")
    parser.add_argument("csv_file", help="Path to CSV file")
    parser.add_argument(
        "--mode",
        choices=["add", "upsert", "sync"],
        default="upsert",
        help=(
            "What this script is allowed to do: "
            "'add' = only POST new contacts; "
            "'upsert' = add new + update changed (default, no deletes); "
            "'sync' = full set-diff including DELETE of contacts not in source."
        ),
    )
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")
    args = parser.parse_args()

    required = ["SMSGLOBAL_KEY", "SMSGLOBAL_SECRET"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    if not os.path.isfile(args.csv_file):
        print(f"❌ CSV file not found: {args.csv_file}")
        sys.exit(1)

    sms_key = os.environ["SMSGLOBAL_KEY"]
    sms_secret = os.environ["SMSGLOBAL_SECRET"]

    print("\n==================================================")
    print("🌍 ## SMSGlobal Sync (CSV) — Target Summary")
    print("==================================================")
    print(f"CSV file:       {args.csv_file}")
    print(f"Target group:   {smsglobal_group}")
    print(f"Mode:           {args.mode}")

    try:
        csv_contacts = read_csv_contacts(args.csv_file)
    except Exception as e:
        print(f"\n❌ Failed to read CSV: {e}")
        sys.exit(1)

    print(f"Usable rows:    {len(csv_contacts)}")
    if not csv_contacts:
        print("\n❌ No rows with usable MOBILE_PHONE found. Aborting.")
        sys.exit(1)

    if not args.yes:
        print("\n==================================================")
        print("📝 ## Final Confirmation")
        print("==================================================")
        if args.mode == "sync":
            print(
                "⚠️  DESTRUCTIVE: mode=sync — contacts present in the group "
                "but NOT in the CSV will be DELETED."
            )
        elif args.mode == "upsert":
            print("Mode=upsert: will add new contacts and update changed ones. No deletes.")
        else:
            print("Mode=add: will only add new contacts. Existing contacts untouched.")
        resp = input(
            f"About to run mode={args.mode} with {len(csv_contacts)} contact(s) "
            f"against '{smsglobal_group}'. Type 'Proceed' to go ahead: "
        )
        if resp.strip() != "Proceed":
            print("Did not receive exact 'Proceed' confirmation. Aborting.")
            sys.exit(0)

    client = SMSGlobalClient(sms_key, sms_secret)
    start = datetime.now()
    print(f"\n🔄 Sync started at {start:%Y-%m-%d %H:%M:%S}")

    stats = sync_group(client, smsglobal_group, csv_contacts, args.mode)

    end = datetime.now()
    print("\n==================================================")
    print("📊 ## Sync Summary")
    print("==================================================")
    print(f"Duration: {end - start}")
    print(
        f"Added: {stats['added']}  Updated: {stats['updated']}  "
        f"Deleted: {stats['deleted']}  Skipped(dupes): {stats['skipped']}  "
        f"Errors: {stats['errors']}"
    )
    sys.exit(1 if stats["errors"] else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🛑 Interrupted by user. Exiting.")
        sys.exit(130)
