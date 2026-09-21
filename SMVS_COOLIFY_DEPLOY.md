# SMVS Chopda-Pujan - Coolify Deployment Handoff

## Decision

**Status: PASS for a NEW deployment after the mandatory prechecks below.**

**Database decision: MATCH-DB01**

- Repository database evidence: `docker-compose.yml` originally used `postgres:16-alpine`; the application uses SQLAlchemy + `psycopg2-binary`.
- Production target: DB01 PostgreSQL 16 on `192.168.40.10:5432`.
- Proposed dedicated database: `chopdapujan_prod`.
- Proposed dedicated least-privilege login: `chopdapujan_app`.
- The production compose no longer starts a local PostgreSQL container and no longer defines a `pgdata` volume.

## Build/runtime

- Build method: Docker Compose -> Dockerfile.
- Runtime: Python 3.11 / Flask / Gunicorn.
- Internal application port: `3000`.
- Health endpoint: `/healthz` (checks a real database round-trip).
- Runtime identity: UID/GID `10001:10001` (pinned in the Dockerfile).
- Redis/queue/search dependency: none found in this repository.

## Persistent media decision

User-generated poster images and uploaded manuals are now written below:

- Container: `/app/media`
- PROD-1 host: `/srv/media/projects/smvs-chopdapujan/media`

The compose file contains this bind. Built-in PDFs shipped in `manuals/` remain immutable image/code assets. Existing database BLOB media remains readable as a compatibility fallback; if an existing application DB is ever adopted, use `ops/migrate_media_blobs.py` only as a separately approved migration after backup and media RW verification.

## 1. Mandatory PROD-1 storage precheck

Run on PROD-1 before creating the media directory:

```bash
findmnt -T /srv/media -o TARGET,SOURCE,FSTYPE,OPTIONS
```

PASS only if it shows:

- source `192.168.10.10:/smvs_hosting_server_live_media`
- filesystem `nfs4`

Then create the project directory with the pinned runtime identity:

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /srv/media/projects/smvs-chopdapujan/media
ls -ldn /srv/media/projects/smvs-chopdapujan/media
```

Expected numeric owner: `10001 10001`.

STOP if `/srv/media` is not the expected QNAP NFS mount or if the directory cannot be prepared for UID/GID 10001.

## 2. Mandatory DB01 connectivity precheck

From PROD-1:

```bash
nc -vz 192.168.40.10 5432
```

Expected: TCP connection succeeds.

STOP if port 5432 is not reachable from PROD-1.

## 3. Provision the dedicated PostgreSQL 16 DB/user on DB01

On DB01, open PostgreSQL 16 as the local postgres admin:

```bash
sudo -u postgres /usr/lib/postgresql/16/bin/psql -p 5432 -d postgres
```

Then use the included `ops/db01-provision.sql` as the template. The password is entered interactively with `\password`; never store it in this repository or documentation.

After provisioning, from PROD-1 verify a real authenticated connection (enter the password interactively):

```bash
psql -h 192.168.40.10 -p 5432 -U chopdapujan_app -d chopdapujan_prod \
  -c "select version(), current_database(), current_user;"
```

Expected database/user: `chopdapujan_prod` / `chopdapujan_app`.

## 4. Coolify resource

Create a Docker Compose resource from this deployment-ready repository/ZIP and use the root `docker-compose.yml`.

Configure the Coolify domain to route HTTPS traffic to service `web`, internal port `3000`.

The compose uses `expose: 3000`, not a public host port mapping. The database is not exposed or started by the compose.

## 5. Coolify environment variables

### Required protected secrets

```text
DATABASE_URL=postgresql://chopdapujan_app:<URL-ENCODED-PASSWORD>@192.168.40.10:5432/chopdapujan_prod
SECRET_KEY=<random long value>
ADMIN_PASSWORD=<strong first-login value>
SANT_PASSWORD=<strong first-login value>
```

Generate `SECRET_KEY` locally, for example:

```bash
python -c "import secrets; print(secrets.token_urlsafe(48))"
```

Also store these as protected values when used:

```text
SMTP_PASSWORD
TEXTGURU_PASSWORD
WHATSAPP_API_KEY
```

Treat other provider credentials such as `SMTP_USER`, `TEXTGURU_LOGINID`, entity/template IDs and sender values according to your normal secret policy.

### Required first deployment settings

For a **new empty dedicated database only**:

```text
DB_SCHEMA_MODE=bootstrap
LEGACY_ALTERS=0
MEDIA_ROOT=/app/media
COOKIE_SECURE=1
BEHIND_PROXY=1
PUBLIC_BASE_URL=https://<final-domain>
```

`DB_SCHEMA_MODE=bootstrap` performs the Alembic schema creation and application seed before Gunicorn starts. Do not use it against an existing application database without a separate migration review.

After the first deployment is healthy and the application has been verified, change:

```text
DB_SCHEMA_MODE=none
```

and redeploy. Future schema changes should be performed as controlled migrations, not silently on every restart.

Optional integration/behaviour variables are documented in `.env.example`.

## 6. First deployment expected result

On first boot with `DB_SCHEMA_MODE=bootstrap`:

1. DB01 becomes reachable.
2. Alembic upgrades the new empty DB to the repository migration head.
3. Initial application seed runs.
4. Gunicorn starts on port 3000.
5. `/healthz` returns HTTP 200 when the DB round-trip succeeds.

STOP if Alembic reports an unexpected pre-existing schema/table state. Do not stamp or force through an existing database without review.

## 7. Post-deploy verification

### Container and health

Use Coolify's terminal or the server's Docker CLI to identify the running `web` container, then verify:

```bash
curl -fsS http://127.0.0.1:3000/healthz
```

If the host port is not published (normal for this compose), execute the curl inside the container instead:

```bash
docker exec <web-container> curl -fsS http://127.0.0.1:3000/healthz
```

Expected: JSON status `ok`.

### Verify the media mount

```bash
docker inspect <web-container> --format '{{range .Mounts}}{{.Source}}|{{.Destination}}|{{.RW}}{{println}}{{end}}'
```

Expected entry:

```text
/srv/media/projects/smvs-chopdapujan/media|/app/media|true
```

### Verify media RW as the real runtime identity

```bash
docker exec -u 10001:10001 <web-container> sh -lc \
  'set -e; f=/app/media/.smvs-rw-test-$$; echo ok > "$f"; test "$(cat "$f")" = ok; mv "$f" "$f.renamed"; rm "$f.renamed"'
```

Expected: exit code 0, and no test file remains.

Then confirm on PROD-1 that `/srv/media/projects/smvs-chopdapujan/media` is still the QNAP-backed path.

### Verify DB01 target and no local DB container

Inside the web container:

```bash
docker exec <web-container> python - <<'PY'
import os
from sqlalchemy import create_engine, text
u = os.environ['DATABASE_URL']
with create_engine(u).connect() as c:
    r = c.execute(text('select current_database(), current_user, inet_server_addr(), inet_server_port(), version()')).one()
    print(r)
PY
```

Expected server: `192.168.40.10`, port `5432`, dedicated DB/user, PostgreSQL 16.

Check the Coolify application has only the `web` service from this compose; there must be no local relational DB container and no `pgdata` volume.

### Functional media test

From the application UI, upload a small test poster or manual, then confirm a new file appears under:

```text
/srv/media/projects/smvs-chopdapujan/media/posters/...
```

or:

```text
/srv/media/projects/smvs-chopdapujan/media/manuals/...
```

Redeploy the app and verify the uploaded file still opens. Remove the test item from the application afterwards.

## 8. Existing DB / legacy media warning

This package is immediately ready for a **new empty dedicated database**. If you intend to attach an existing Chopda-Pujan database instead:

- do not use `DB_SCHEMA_MODE=bootstrap` until its schema/Alembic state is reviewed;
- take the database rollback point first;
- apply the `d1e6f2a4c8b0` media-path migration in a controlled change;
- verify the NAS bind RW test;
- only then run `python ops/migrate_media_blobs.py` if existing posters/manuals are stored as DB BLOBs;
- verify files open correctly before considering the BLOB migration complete.

## 9. Rollback

Before cutover, retain the previous Coolify compose/environment settings and a database rollback point.

Application rollback:

1. Stop/redeploy only this Coolify application to the previous image/revision.
2. Restore its previous environment/storage settings.
3. Do not delete `/srv/media/projects/smvs-chopdapujan/media`; it is persistent user data.
4. Do not drop the DB01 database/user as part of an application rollback unless a separate approved DB rollback requires it.

If the first deployment used a brand-new, empty DB and failed before production data was accepted, the DB can be separately reviewed for cleanup. Never drop it automatically from the application deployment workflow.

## 10. STOP conditions

Stop rather than forcing deployment if any of these occur:

- `/srv/media` is not the expected QNAP NFS source/type.
- UID/GID 10001 cannot create/read/rename/delete through the mounted media path.
- DB01 `192.168.40.10:5432` is unreachable from PROD-1.
- The DB is not PostgreSQL 16 or is not a new/approved existing database.
- Alembic reports an unexpected existing schema state.
- Coolify starts any local relational DB service for this app.
- `DATABASE_URL` points anywhere other than the approved DB01 target.
- production secrets appear in the repository, ZIP, compose file, or logs.
- uploaded media does not survive a redeploy.

## Security note about the supplied source ZIP

The supplied source `.env.example` contained credential-like values rather than placeholders. This deployment-ready copy has removed them. Treat any such original values as exposed and rotate/revoke them in the relevant providers before production use. Do not copy them into Coolify.
