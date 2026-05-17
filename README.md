# SMSGlobal API Examples

A growing collection of practical examples for working with the
[SMSGlobal REST API](https://www.smsglobal.com/rest-api/). Each script is
self-contained and demonstrates a specific integration scenario.

## Operating modes

All three contact-management scripts share a `--mode` flag that controls
which write operations the script is allowed to perform:

| Mode     | Add new | Update changed | Delete missing | Notes                                          |
| -------- | :-----: | :------------: | :------------: | ---------------------------------------------- |
| `add`    |   ✅    |       —        |       —        | Safest. Only inserts new contacts.             |
| `upsert` |   ✅    |       ✅       |       —        | **Default.** Add new + update changed fields.  |
| `sync`   |   ✅    |       ✅       |       ✅       | Destructive: deletes contacts not in source.   |

The scripts print a target summary and ask for explicit `Proceed`
confirmation before issuing any writes. The confirmation prompt is
stronger when `--mode sync` is selected. Pass `--yes` to skip the prompt
for automation. Updates are only sent when a mapped field actually
changed.

## Scripts

### [src/smsglobal-contacts-db2.py](src/smsglobal-contacts-db2.py) — DB2 source

Pushes contacts from IBM DB2 VIEWs into SMSGlobal groups. For each
configured school × view pair, it ensures the target SMSGlobal group
exists (`<SCHOOL>_<GROUP>` naming), pages through the group's existing
contacts, reads contacts from the corresponding DB2 VIEW, and applies
add / update / delete operations per `--mode`.

Highlights:

- **MAC authentication** (HMAC-SHA256) as required by the SMSGlobal API — the
  Authorization header is re-signed on every request with a fresh `ts` and
  `nonce`.
- **429 rate-limit handling** with `Retry-After` support and exponential
  backoff (up to 5 attempts).
- **Per-contact error isolation** — a single failed add/update/delete is
  logged and the run continues with the next contact.
- **Graceful exit** on Ctrl+C (exit code 130).
- **Phone normalisation** for Australian and international numbers so the
  same person isn't seen as two different contacts due to formatting
  (`0412 345 678` vs `+61412345678` vs `61412345678`).

```powershell
python src/smsglobal-contacts-db2.py [--env prod|dev] [--mode add|upsert|sync] [--yes]
```

### [src/smsglobal-contacts-postgres.py](src/smsglobal-contacts-postgres.py) — PostgreSQL source

Same logic as the DB2 script above, but reads from PostgreSQL via
[psycopg](https://www.psycopg.org/) v3. Source VIEWs are expected to expose
the same columns (`firstname, surname, display_name, mobile_phone,
email_address`). Postgres connection settings live in `postgres_db_prod` /
`postgres_db_dev` in [config.py](config.py); credentials come from `PG_USER`
and `PG_PWD` in `.env`.

```powershell
python src/smsglobal-contacts-postgres.py [--env prod|dev] [--mode add|upsert|sync] [--yes]
```

### [src/smsglobal-contacts-csv.py](src/smsglobal-contacts-csv.py) — CSV source

A simpler variant that reads contacts from a CSV file and writes them into
a single configurable SMSGlobal group (`smsglobal_group` constant at the
top of the script). CSV headers are matched case-insensitively and must
include `MOBILE_PHONE`; the other four (`FIRSTNAME`, `SURNAME`,
`DISPLAY_NAME`, `EMAIL_ADDRESS`) are optional.

```powershell
python src/smsglobal-contacts-csv.py path/to/contacts.csv [--mode add|upsert|sync] [--yes]
```

## Setup

1. Install dependencies:

   ```powershell
   pip install ibm_db "psycopg[binary]" requests python-dotenv
   ```

2. Copy `.env.example` to `.env` and fill in your credentials:

   ```dotenv
   DB_USER=...
   DB_PWD=...
   DB_ENV=prod
   PG_USER=...
   PG_PWD=...
   SMSGLOBAL_KEY=...
   SMSGLOBAL_SECRET=...
   ```

3. Edit [config.py](config.py) to point at your DB hosts, and adjust the
   `include_update` list and `db2_view_to_smsglobal_group_mapping` inside the
   relevant script.

## Notes

- The SMSGlobal MAC authentication scheme is time-sensitive — keep your
  machine's clock in sync (NTP) or authentication will fail.
- Nonces must be unique per request; the included client uses
  `secrets.token_hex(16)`.

## License

[MIT](LICENSE)
