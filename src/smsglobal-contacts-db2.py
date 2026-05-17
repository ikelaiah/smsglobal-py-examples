"""Push Edumate DB2 VIEW contacts into SMSGlobal groups.

Usage:
    python src/smsglobal-contacts-db2.py [--env prod|dev] [--mode add|upsert|sync] [--yes]

Modes:
  add    - only POST new contacts (existing contacts untouched)
  upsert - add new + update changed contacts (default; no deletes)
  sync   - full set-diff including DELETE of contacts not in the source

Per (school, view) pair:
  1. Ensure SMSGlobal group "<SCHOOL>_<MAPPED_GROUP>" exists.
  2. Build Array A from SMSGlobal (paginated GET, normalised msisdns).
  3. Build Array B from the DB2 VIEW on that school's DB (normalised phones).
  4. Apply add / update / delete operations gated by --mode.

Failures on individual contact operations are logged and the run continues.
"""

import os
import sys
import re
import argparse
import pathlib
from datetime import datetime

# ─────────────────────────────────────────────────────────────────────────────
# IBM DB2 driver DLL setup
# ─────────────────────────────────────────────────────────────────────────────
import site
_candidates = []
for _sp in site.getsitepackages():
    _sp = pathlib.Path(_sp)
    _candidates.append(_sp / "clidriver" / "bin")
    _candidates.append(_sp / "ibm_db" / "clidriver" / "bin")

_added = False
for _p in _candidates:
    if _p.exists():
        os.add_dll_directory(str(_p))
        _added = True
        break

if not _added:
    _ibm_home = os.environ.get("IBM_DB_HOME")
    if _ibm_home:
        _binp = pathlib.Path(_ibm_home) / "bin"
        if _binp.exists():
            os.add_dll_directory(str(_binp))
    else:
        print("WARNING: Could not locate IBM DB2 CLI driver DLLs.", file=sys.stderr)

import time  # noqa: E402
import hmac  # noqa: E402
import hashlib  # noqa: E402
import base64  # noqa: E402
import secrets  # noqa: E402
from urllib.parse import urlsplit  # noqa: E402

import ibm_db  # noqa: E402
import requests  # noqa: E402
from dotenv import load_dotenv  # noqa: E402

# config.py lives one level up from src/
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))
from config import edu_institution_db_prod, edu_institution_db_dev  # noqa: E402


# ─────────────────────────────────────────────────────────────────────────────
# Configuration
# ─────────────────────────────────────────────────────────────────────────────

# Education Institutions to include
include_update = ['SCHOOL01', 'SCHOOL02']

# Mapping of DB2 view -> base SMSGlobal group name.
# Group names are prefixed with the school code at runtime,
# e.g. SCHOOL01_SMSGLOBAL_GROUP_1.
db2_view_to_smsglobal_group_mapping = {
    "DB2_VIEW_1": "SMSGLOBAL_GROUP_1",
    "DB2_VIEW_2": "SMSGLOBAL_GROUP_2",
    "DB2_VIEW_3": "SMSGLOBAL_GROUP_3",
}

# DB2 column -> SMSGlobal field
db2_columns_to_match = {
    "FIRSTNAME": "givenName",
    "SURNAME": "familyName",
    "DISPLAY_NAME": "displayName",
    "MOBILE_PHONE": "msisdn",
    "EMAIL_ADDRESS": "emailAddress",
}

# Fields compared on update (excludes the matching key msisdn)
COMPARE_FIELDS = ["givenName", "familyName", "displayName", "emailAddress"]

SMSGLOBAL_BASE = "https://api.smsglobal.com/v2"
DEFAULT_TIMEOUT = 30


# ─────────────────────────────────────────────────────────────────────────────
# Phone normalisation
# ─────────────────────────────────────────────────────────────────────────────

def normalise_phone(raw):
    """Normalise a phone number to international form without leading '+'.

    Australian rules:
      - 04xxxxxxxx (10 digits, leading 0) -> 614xxxxxxxx
      - 4xxxxxxxx  (9 digits, no leading 0) -> 614xxxxxxxx
      - +61 / 0061 prefixes -> 61...
    Overseas numbers retain their country code as supplied.
    Returns None if the input is empty or has no digits.
    """
    if raw is None:
        return None
    s = str(raw).strip()
    if not s:
        return None

    # Strip all non-digit characters except a leading '+'
    has_plus = s.startswith("+")
    digits = re.sub(r"\D", "", s)
    if not digits:
        return None

    # 00<cc>... → <cc>...
    if digits.startswith("00"):
        digits = digits[2:]
        has_plus = True

    if has_plus:
        return digits

    # No '+': apply Australian heuristics
    if digits.startswith("61"):
        return digits
    if digits.startswith("0"):
        # Local AU form, e.g. 0412345678 -> 61412345678
        return "61" + digits[1:]
    if len(digits) == 9 and digits.startswith("4"):
        # 9-digit AU mobile without leading 0
        return "61" + digits
    # Fallback: return digits as-is (best effort for overseas without '+')
    return digits


# ─────────────────────────────────────────────────────────────────────────────
# DB2 helpers
# ─────────────────────────────────────────────────────────────────────────────

def _sanitize(value):
    return str(value).replace(";", "")


def build_conn_str(host, port, dbname, user, pwd, schema=None):
    schema_part = f"CURRENTSCHEMA={_sanitize(schema)};" if schema else ""
    return (
        f"DATABASE={_sanitize(dbname)};"
        f"HOSTNAME={_sanitize(host)};"
        f"PORT={_sanitize(str(port))};"
        f"PROTOCOL=TCPIP;"
        f"UID={_sanitize(user)};"
        f"PWD={_sanitize(pwd)};"
        f"{schema_part}"
    )


def fetch_view_contacts(conn, view_name):
    """SELECT all contact rows from a VIEW, skipping rows with no MOBILE_PHONE.

    Returns a list of dicts keyed by SMSGlobal field names, plus a normalised
    msisdn under the 'msisdn' key.
    """
    sql = (
        f"SELECT FIRSTNAME, SURNAME, DISPLAY_NAME, MOBILE_PHONE, EMAIL_ADDRESS "
        f"FROM {view_name}"
    )
    stmt = ibm_db.exec_immediate(conn, sql)
    contacts = []
    row = ibm_db.fetch_assoc(stmt)
    while row:
        phone = normalise_phone(row.get("MOBILE_PHONE"))
        if phone:
            contacts.append({
                "msisdn": phone,
                "givenName": (row.get("FIRSTNAME") or "").strip() or None,
                "familyName": (row.get("SURNAME") or "").strip() or None,
                "displayName": (row.get("DISPLAY_NAME") or "").strip() or None,
                "emailAddress": (row.get("EMAIL_ADDRESS") or "").strip() or None,
            })
        row = ibm_db.fetch_assoc(stmt)
    return contacts


# ─────────────────────────────────────────────────────────────────────────────
# SMSGlobal client
# ─────────────────────────────────────────────────────────────────────────────

class SMSGlobalClient:
    """SMSGlobal REST client using MAC (HMAC-SHA256) authorization.

    Per https://www.smsglobal.com/rest-api/, the Authorization header is built
    from: id, ts, nonce, mac. The MAC is a base64-encoded HMAC-SHA256 over:
        ts\nnonce\nMETHOD\nURI\nHOST\nPORT\n\n
    keyed by the API secret. Each nonce must be unique per request.
    """

    def __init__(self, key, secret):
        self.key = key
        self.secret = secret.encode("utf-8") if isinstance(secret, str) else secret
        self.session = requests.Session()

    def _auth_header(self, method, url):
        parts = urlsplit(url)
        host = parts.hostname or ""
        port = parts.port or (443 if parts.scheme == "https" else 80)
        # URI must include the path AND query string as sent on the wire
        uri = parts.path or "/"
        if parts.query:
            uri = f"{uri}?{parts.query}"

        ts = str(int(time.time()))
        nonce = secrets.token_hex(16)  # 32 hex chars
        mac_input = f"{ts}\n{nonce}\n{method}\n{uri}\n{host}\n{port}\n\n"
        digest = hmac.new(self.secret, mac_input.encode("utf-8"), hashlib.sha256).digest()
        mac = base64.b64encode(digest).decode("ascii")
        return (
            f'MAC id="{self.key}", ts="{ts}", nonce="{nonce}", mac="{mac}"'
        )

    def _request(self, method, path, **kwargs):
        url = f"{SMSGLOBAL_BASE}{path}"
        # requests will reorder/normalise query params from `params=`; build the
        # final URL up front so the signed URI matches what's actually sent.
        if "params" in kwargs and kwargs["params"]:
            prepared = requests.Request(method, url, params=kwargs.pop("params")).prepare()
            url = prepared.url
        kwargs.setdefault("timeout", DEFAULT_TIMEOUT)
        body_headers = kwargs.pop("headers", {}) or {}

        max_attempts = 5
        backoff = 2.0
        for attempt in range(1, max_attempts + 1):
            headers = dict(body_headers)
            # Re-sign on every attempt: ts/nonce must be fresh per request.
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

    # Groups
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

    # Contacts
    def list_group_contacts(self, group_id):
        contacts = []
        offset = 0
        limit = 20  # SMSGlobal default/max per page
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
    """Drop None fields so we send only what we have."""
    return {k: v for k, v in db_contact.items() if v is not None}


def _needs_update(remote, local):
    for field in COMPARE_FIELDS:
        if (remote.get(field) or None) != (local.get(field) or None):
            return True
    return False


def sync_group(client, group_name, db_conn, view_name, school_label, mode):
    """Sync one DB2 VIEW into one SMSGlobal group. Returns a stats dict.

    mode: "add" | "upsert" | "sync"
        add    -> only POST new contacts (no updates, no deletes)
        upsert -> add new + update changed (no deletes)
        sync   -> add new + update changed + delete missing
    """
    stats = {"added": 0, "updated": 0, "deleted": 0, "skipped": 0, "errors": 0}

    print(f"\n── [{school_label}] {view_name} → {group_name} (mode={mode})")

    # 1) Resolve / create group
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

    # 2a) Array A — SMSGlobal contacts
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

    # 2b) Array B — DB2
    try:
        db_contacts = fetch_view_contacts(db_conn, view_name)
    except Exception as e:
        print(f"  ❌ Failed to query VIEW {view_name}: {e}")
        stats["errors"] += 1
        return stats

    # Deduplicate by phone in case the VIEW has dupes
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

    # 2c) Add
    for phone in to_add:
        payload = _build_payload(local_by_phone[phone])
        try:
            client.add_contact(group_id, payload)
            stats["added"] += 1
        except Exception as e:
            print(f"    ❌ add {phone}: {e}")
            stats["errors"] += 1

    # 2d) Update (only if a compared field changed) — upsert + sync only
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

    # 2e) Delete — sync only
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

    parser = argparse.ArgumentParser(description="Sync DB2 VIEW contacts into SMSGlobal groups.")
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

    # Required env vars
    required = ["DB_USER", "DB_PWD", "SMSGLOBAL_KEY", "SMSGLOBAL_SECRET"]
    missing = [v for v in required if not os.environ.get(v)]
    if missing:
        print(f"❌ Missing required environment variables: {', '.join(missing)}")
        sys.exit(1)

    db_user = os.environ["DB_USER"]
    db_pwd = os.environ["DB_PWD"]
    db_schema = os.environ.get("DB_SCHEMA")
    sms_key = os.environ["SMSGLOBAL_KEY"]
    sms_secret = os.environ["SMSGLOBAL_SECRET"]

    db_env = (args.env or os.environ.get("DB_ENV", "prod")).lower()
    if db_env in ("dev", "development"):
        targets = edu_institution_db_dev
        env_label = "development"
        env_emoji = "🧪"
    else:
        targets = edu_institution_db_prod
        env_label = "production"
        env_emoji = "🚨"

    # Restrict to configured schools
    targets = {name: cfg for name, cfg in targets.items() if name in include_update}

    print("\n==================================================")
    print("🌍 ## SMSGlobal Sync — Target Summary")
    print("==================================================")
    print(f"Environment: {env_label} {env_emoji}")
    print(f"Mode:        {args.mode}")
    print(f"Schools:     {', '.join(targets.keys()) or '(none)'}")
    print(f"Views → base groups:")
    for view, group in db2_view_to_smsglobal_group_mapping.items():
        print(f"  - {view} → <SCHOOL>_{group}")
    if not targets:
        print("\n❌ No schools to sync (include_update is empty or doesn't match config).")
        sys.exit(1)

    if not args.yes:
        print("\n==================================================")
        print("📝 ## Final Confirmation")
        print("==================================================")
        if args.mode == "sync":
            print(
                "⚠️  DESTRUCTIVE: mode=sync — contacts present in the SMSGlobal "
                "groups but NOT in the source DB2 VIEWs will be DELETED."
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
        conn_str = build_conn_str(cfg["host"], cfg["port"], cfg.get("db_name"), db_user, db_pwd, db_schema)
        conn = None
        try:
            conn = ibm_db.connect(conn_str, "", "")
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
                ibm_db.close(conn)
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
