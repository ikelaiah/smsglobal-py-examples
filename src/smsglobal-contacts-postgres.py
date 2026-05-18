"""Push PostgreSQL VIEW contacts into SMSGlobal groups.

Parallel to smsglobal-contacts-db2.py but reads from PostgreSQL via
psycopg (v3).

Usage:
    python src/smsglobal-contacts-postgres.py [--env prod|dev] [--mode add|upsert|sync] [--yes]

Modes:
  add    - only POST new contacts (existing contacts untouched)
  upsert - add new + update changed contacts (default; no deletes)
  sync   - full set-diff including DELETE of contacts not in the source

Source VIEWs are expected to expose the same columns as the DB2 example:
firstname, surname, display_name, mobile_phone, email_address.
"""

import os
import sys
import re
import argparse
import pathlib
import time
import hmac
import hashlib
import base64
import secrets
from datetime import datetime
from urllib.parse import urlsplit

try:
    import psycopg
except ImportError as e:
    print(
        f"psycopg import failed: {e}\n"
        "Install with:  pip install 'psycopg[binary]'",
        file=sys.stderr,
    )
    raise

import requests
from dotenv import load_dotenv

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import postgres_db_prod, postgres_db_dev  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

include_update = ['SCHOOL01', 'SCHOOL02']

db2_view_to_smsglobal_group_mapping = {
    "db2_view_1": "SMSGLOBAL_GROUP_1",
    "db2_view_2": "SMSGLOBAL_GROUP_2",
    "db2_view_3": "SMSGLOBAL_GROUP_3",
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
# Postgres helpers
# ─────────────────────────────────────────────────────────────────────────────

# Match a safe SQL identifier: letters, digits, underscores, optional schema-qualified
_IDENT_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*(\.[A-Za-z_][A-Za-z0-9_]*)?$")


def _safe_view_name(name):
    if not _IDENT_RE.match(name):
        raise ValueError(f"Unsafe view identifier: {name!r}")
    return name


def fetch_view_contacts(conn, view_name):
    """SELECT all contact rows from a VIEW, skipping rows with no MOBILE_PHONE."""
    safe = _safe_view_name(view_name)
    sql = (
        f"SELECT firstname, surname, display_name, mobile_phone, email_address "
        f"FROM {safe}"
    )
    contacts = []
    with conn.cursor() as cur:
        cur.execute(sql)
        for firstname, surname, display_name, mobile_phone, email_address in cur:
            phone = normalise_phone(mobile_phone)
            if not phone:
                continue
            contacts.append({
                "msisdn": phone,
                "givenName": (firstname or "").strip() or None,
                "familyName": (surname or "").strip() or None,
                "displayName": (display_name or "").strip() or None,
                "emailAddress": (email_address or "").strip() or None,
            })
    return contacts


def pg_connect(cfg, user, pwd):
    return psycopg.connect(
        host=cfg["host"],
        port=cfg["port"],
        dbname=cfg["db_name"],
        user=user,
        password=pwd,
        connect_timeout=10,
    )


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

def _build_payload(db_contact):
    return {k: v for k, v in db_contact.items() if v is not None}


def _needs_update(remote, local):
    for field in COMPARE_FIELDS:
        if (remote.get(field) or None) != (local.get(field) or None):
            return True
    return False


def sync_group(client, group_name, db_conn, view_name, school_label, mode):
    """mode: "add" | "upsert" | "sync" — see module docstring."""
    stats = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}
    print(f"\n── [{school_label}] {view_name} → {group_name} (mode={mode})")

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

    try:
        db_contacts = fetch_view_contacts(db_conn, view_name)
    except Exception as e:
        print(f"  ❌ Failed to query VIEW {view_name}: {e}")
        stats["errors"] += 1
        return stats

    local_by_phone = {}
    for c in db_contacts:
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
        f"  Remote: {len(remote_by_phone)}  Local: {len(local_by_phone)}  "
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

    parser = argparse.ArgumentParser(description="Sync PostgreSQL VIEW contacts into SMSGlobal groups.")
    parser.add_argument("--env", choices=["prod", "dev"], help="Environment (default: from DB_ENV, then prod)")
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

    required = ["PG_USER", "PG_PWD", "SMSGLOBAL_KEY", "SMSGLOBAL_SECRET"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    pg_user = os.environ["PG_USER"]
    pg_pwd = os.environ["PG_PWD"]
    sms_key = os.environ["SMSGLOBAL_KEY"]
    sms_secret = os.environ["SMSGLOBAL_SECRET"]

    db_env = (args.env or os.environ.get("DB_ENV", "prod")).lower()
    if db_env in ("dev", "development"):
        targets = postgres_db_dev
        env_label = "development"
        env_emoji = "🧪"
    else:
        targets = postgres_db_prod
        env_label = "production"
        env_emoji = "🚨"

    targets = {name: cfg for name, cfg in targets.items() if name in include_update}

    print("\n==================================================")
    print("🌍 ## SMSGlobal Sync (Postgres) — Target Summary")
    print("==================================================")
    print(f"Environment: {env_label} {env_emoji}")
    print(f"Mode:        {args.mode}")
    print(f"Schools:     {', '.join(targets.keys()) or '(none)'}")
    print("Views → base groups:")
    for view, group in db2_view_to_smsglobal_group_mapping.items():
        print(f"  - {view} → <SCHOOL>_{group}")
    if not targets:
        print("\n❌ No schools to sync.")
        sys.exit(1)

    if not args.yes:
        print("\n==================================================")
        print("📝 ## Final Confirmation")
        print("==================================================")
        if args.mode == "sync":
            print(
                "⚠️  DESTRUCTIVE: mode=sync — contacts present in the SMSGlobal "
                "groups but NOT in the source VIEWs will be DELETED."
            )
        elif args.mode == "upsert":
            print("Mode=upsert: will add new contacts and update changed ones. No deletes.")
        else:
            print("Mode=add: will only add new contacts. Existing contacts untouched.")
        resp = input(
            f"About to run mode={args.mode} on {len(targets)} school(s) × "
            f"{len(db2_view_to_smsglobal_group_mapping)} view(s) in {env_label}. "
            f"Type 'Proceed' to go ahead: "
        )
        if resp.strip() != "Proceed":
            print("Did not receive exact 'Proceed' confirmation. Aborting.")
            sys.exit(0)

    client = SMSGlobalClient(sms_key, sms_secret)
    start = datetime.now()
    print(f"\n🔄 Sync started at {start:%Y-%m-%d %H:%M:%S}")

    totals = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}

    for school_name, cfg in targets.items():
        print(f"\n══ {school_name} ({cfg['host']}:{cfg['port']} / {cfg.get('db_name')}) ══")
        try:
            conn = pg_connect(cfg, pg_user, pg_pwd)
        except Exception as e:
            print(f"  ❌ DB connect failed: {e}")
            totals["errors"] += 1
            continue

        try:
            for view_name, base_group_name in db2_view_to_smsglobal_group_mapping.items():
                group_name = f"{school_name}_{base_group_name}"
                stats = sync_group(client, group_name, conn, view_name, school_name, args.mode)
                for k in totals:
                    totals[k] += stats.get(k, 0)
        finally:
            try:
                conn.close()
            except Exception:
                pass

    end = datetime.now()
    print("\n==================================================")
    print("📊 ## Sync Summary")
    print("==================================================")
    print(f"Duration: {end - start}")
    print(
        f"Added: {totals['added']}  Updated: {totals['updated']}  "
        f"Deleted: {totals['deleted']}  Skipped(dupes): {totals['skipped']}  "
        f"Errors: {totals['errors']}"
    )
    sys.exit(1 if totals["errors"] else 0)


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\n\n🛑 Interrupted by user. Exiting.")
        sys.exit(130)
