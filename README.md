# Single merchant store

A persisted single-merchant MVC store with customer accounts, merchant MFA, catalog management, carts, orders and returns. Payment, shipping, tax and order-notification providers remain deterministic simulations. Customer verification/reset email is separate; the default captures it in a protected local inbox.

## Start the local demonstration

Download or clone this repository and install/start Docker with Compose. From the repository root, run:

```sh
docker compose up --build
```

The first build downloads images and locked dependencies. It packages Python 3.12.15, Node 24.22.1/npm 10.2.4, PostgreSQL 16.15 and Caddy 2.11.7; host language/database/proxy installations and external service credentials are unnecessary.

Wait for the `READY` message and healthy service status. Startup owns PostgreSQL, migrations, guarded first-use initialization, unique local accounts/MFA, six seeded products, the renderer, worker, HTTP application, mail capture and HTTPS proxy. It reports readiness only after verified HTTPS health and a real rendered storefront response.

Open **https://localhost:8443**, with merchant tools at **https://localhost:8443/merchant**. Only host loopback 8443 is published. Internal PostgreSQL/HTTP/SMTP ports stay inside the container.

## First-use certificate and accounts

Export the public local CA certificate:

```sh
docker compose cp store:/state/caddy-data/caddy/pki/authorities/local/root.crt ./store-local-root.crt
```

Explicitly approve this local CA in the browser you use. [Mozilla’s Firefox instructions](https://wiki.mozilla.org/CA/Changing_Trust_Settings) describe Settings → Privacy & Security → Certificates → View Certificates → Authorities → Import. Choose `store-local-root.crt` and approve website identification. To remove it, select this imported authority in the same panel, choose Delete or Distrust, and restart Firefox. These GUI steps are guidance; the tested browser-trust path used a private Firefox NSS profile on Apple Silicon macOS. The container does not install host trust or disable TLS verification. Keep the CA private key in `/state`.

For maintainers with the optional NSS `certutil` tool, the tested isolated-profile import procedure is:

```sh
STORE_FIREFOX_PROFILE="$(mktemp -d "${TMPDIR:-/tmp}/store-firefox.XXXXXX")"
certutil -N -d "sql:$STORE_FIREFOX_PROFILE" --empty-password
certutil -A -d "sql:$STORE_FIREFOX_PROFILE" -n store-local-ca -t C,, -i ./store-local-root.crt
```

Launch Firefox with that profile and visit the HTTPS storefront. After closing Firefox, remove that profile’s imported authority with `certutil -D -d "sql:$STORE_FIREFOX_PROFILE" -n store-local-ca`. Qualification verified trusted navigation before removal and rejected HTTPS navigation afterward, without bypassing certificate checks or changing system trust. `certutil` is a maintainer validation option, not a prerequisite for the Docker quickstart.

Deliberately reveal your own generated local-demo access information in an interactive terminal:

```sh
docker compose exec store python manage.py local-access --config /state/profile.json
```

This command intentionally displays local bootstrap passwords and applicable authenticator enrollment details. It validates the current native password/factor bindings and identifies stale bootstrap records; changed passwords cannot be recovered from hashes. Credentials are absent from normal startup logs, public APIs and this repository. Merchant sign-in requires its privately provisioned password and a fresh authenticator code. Clones get independent accounts; the author's native instance is not mounted or imported.

Public customer signup is available at `/register`; verify the email link before signing in at `/account`. `/forgot-password` supports verified self-registered customers. Privately provisioned customers retain username login and private credential recovery; merchants retain private provisioning and MFA.

The protected local email viewer is **https://localhost:8443/local-mail/**. Use username **operator** and the separate capture password intentionally shown by `local-access`. It reuses the existing authenticated capture service, exposes no arbitrary-send endpoint, and does not publish port 8025. Messages marked `LOCAL_CAPTURED` stayed local. `SMTP_ACCEPTED` means only server acceptance, not confirmed inbox delivery. Failed/uncertain delivery is shown as `DELIVERY_FAILED`, with bounded retry of the same message identity. External SMTP configuration is optional; the default needs no mail provider.

## Stop, restart and preserve records

Press **Ctrl-C** in the startup terminal. The supervisor stops its application children, gives PostgreSQL a separate graceful shutdown budget, and preserves the named volume. Later launches reuse the same startup command, database, accounts, local CA, carts and orders. Rebuilding/recreating the container preserves that volume too.

```sh
docker compose down
```

`down` removes stopped containers/network while keeping the named data volume. Do not remove the volume as a normal stop or troubleshooting step. The container applies numbered migrations before serving and renews only expired, matching, non-revoked worker credentials through the supported native port; it never resets customer passwords/MFA or reopens explicitly closed authority gates.

Startup holds an exclusive state lock. Concurrent use of the same state is rejected. Partial initialization or a mismatched binding fails with a phase/code and preserves the state; ordinary startup never deletes or resets it. Restore/reset maintenance still only stages `RECOVERY_HOLD`; automatic reconciliation/reopening is unavailable.

## Inspect a problem

```sh
docker compose ps
docker compose logs --tail=100 store
docker compose exec store python manage.py check --config /state/profile.json
```

Confirm Docker is running and host 8443 is available. An occupied port must be released by its owner. Stop an optional native runner using that port before starting the container; their private data stays separate. Dependency-download/build errors are not successful startup; retry after correcting connectivity. An exited child makes the container fail and stops the other owned services. Keep partial-state volumes for inspection rather than deleting them. Never share local-access output, private profiles, MFA files or CA private keys.

Verified on macOS 15.5 on Apple Silicon, using a Colima/VZ Linux arm64 Docker engine and real Firefox with a private NSS trust profile. Verification covered empty-state startup, customer sign-in and merchant sign-in with MFA, catalog/cart/purchase flows, registration/email verification/password reset, persistent stop/restart/recreation, and the documented failure protections. Certificate import/removal used the isolated-profile procedure above; GUI/system-trust changes were not tested. Other host, architecture and browser combinations remain unverified.

## Source map

| Path | Responsibility |
| --- | --- |
| `app/models/` | Domain rules, persistence, identity and provider simulation |
| `app/controllers/` | HTTP/browser controllers, worker and supervised startup |
| `app/views/` | React pages, components, CSS and public assets |
| `config/` | Build, proxy and placeholder configuration |
| `manage.py` | Private setup, maintenance and local-access commands |
| `Dockerfile`, `compose.yaml` | Self-contained local runtime and persistent state |

<details>
<summary>Maintained manual native setup and SMTP configuration</summary>

The following is an optional existing native workflow; it does not participate in the Docker quickstart. Native state remains separate from the Docker named volume.

A native MVC application with Python models and HTTP controllers, React/TypeScript views, PostgreSQL, a Node renderer, a worker, and Caddy HTTPS. Payments, shipping, tax, and order notifications remain simulated. Customer verification and password-reset emails use a separate durable SMTP queue; the default setup captures those emails locally.

## Prerequisites

Install these tools and make their executables available on `PATH`:

- Python **3.12.15**, including `venv` and `pip`.
- Node **24.22.1** and npm **10.2.4**.
- PostgreSQL **16.15**, including `initdb` and `pg_ctl`.
- Caddy **2.11.7**.

The commands below use a POSIX shell and run from the repository root. Keep private state outside this repository. Defaults use `~/.local/share/single-merchant-store`; ports are PostgreSQL `55439`, application `8080`, HTTPS `8443`, local SMTP `1025`, and the private mail viewer `8025`.

## Upgrade an existing instance

Stop its `manage.py run` runner with **Ctrl-C** and wait for its children to exit. Leave PostgreSQL running. Activate the existing virtual environment, then run:

```sh
. .venv/bin/activate
STORE_STATE="$HOME/.local/share/single-merchant-store"
STORE_PROFILE="$STORE_STATE/profile.json"
python -m pip install --require-hashes -r requirements.lock
python manage.py configure-mail --config "$STORE_PROFILE"
python manage.py migrate --config "$STORE_PROFILE"
npm ci --ignore-scripts
npm run build
python manage.py run --config "$STORE_PROFILE"
```

`configure-mail` adds private local capture settings when missing and preserves an existing mail configuration. Migration and restart preserve existing accounts, shopping records, and certificates. Initialization is a separate first-use operation.

## Create a new instance

Use a new state directory. `configure` creates protected PostgreSQL bootstrap files; it does not start PostgreSQL.

```sh
python3.12 -m venv .venv
. .venv/bin/activate
python -m pip install --require-hashes -r requirements.lock
npm ci --ignore-scripts
npm run build

umask 077
STORE_STATE="$HOME/.local/share/single-merchant-store"
STORE_PROFILE="$STORE_STATE/profile.json"
python manage.py configure --state-dir "$STORE_STATE"
initdb -D "$STORE_STATE/postgres" -U sme_fixture_admin \
  --pwfile="$STORE_STATE/postgres-password" \
  --auth-local=peer --auth-host=scram-sha-256 --encoding=UTF8
pg_ctl -D "$STORE_STATE/postgres" -l "$STORE_STATE/postgres.log" \
  -o "-h 127.0.0.1 -p 55439" start
python manage.py init --state-dir "$STORE_STATE" \
  --admin-database-file "$STORE_STATE/admin-database" \
  --origin https://localhost:8443
python manage.py run --config "$STORE_PROFILE"
```

`init` creates a fresh guarded database, local accounts, MFA factors, worker credentials, and six catalog products. It refuses an already initialized state directory. If initialization fails, preserve that directory and inspect the error before choosing a new one.

The runner starts the renderer, local mail capture, worker, HTTP application, and HTTPS proxy. Keep its terminal open. **Ctrl-C** stops those owned processes while preserving state. Restart with the same `run` command. If PostgreSQL was stopped, start it first using the `pg_ctl` command above.

## HTTPS and accounts

Open **https://localhost:8443**. On first use, import the public local CA certificate into your browser's certificate trust store:

```text
<state>/caddy-data/caddy/pki/authorities/local/root.crt
```

The runner prints this path and does not install system trust. Keep the CA private key in private state.

Customers can create an account at `/register`, open the verification link from their email, then sign in at `/account`. `/forgot-password` sends a one-use reset link; after changing the password, sign in again. Existing customer sessions are invalidated. The verification page also supports requesting a fresh link.

Generated local account usernames and credential-file paths are listed in `<state>/reviewer-setup.json`. Read those private files locally. Merchant accounts require the provisioned password and a current authenticator code; enroll their private TOTP seed in an authenticator. These setup records do not recover subsequently changed passwords. Public email recovery is available to verified, self-registered customers. Existing privately provisioned customers can still sign in with their usernames and use private provisioning for credential recovery. Merchants also use private provisioning and MFA.

## Local mail and external SMTP

The default private inbox is **http://127.0.0.1:8025**. Sign in as **operator**, using the password stored at `<state>/mail-capture-password`. The runner prints the viewer URL and password-file path, never the password. The viewer is separate from the storefront's simulated order-message inbox. Captured emails have not been sent externally.

For external account-email delivery, stop the runner and edit the private profile's `mail` object. Use the shape in `config/profile.example.json`, removing `capture` when selecting `smtp` mode:

```json
{
  "mode": "smtp",
  "public_origin": "https://localhost:8443",
  "from_address": "accounts@example.com",
  "from_name": "Single Merchant Store",
  "host": "smtp.example.com",
  "port": 587,
  "security": "starttls",
  "username": "YOUR_SMTP_USERNAME",
  "password_ref": "file:/absolute/private/smtp-password",
  "timeout_seconds": 3,
  "max_attempts": 8,
  "queue_key_ref": "file:/absolute/private/mail-queue-key"
}
```

Use your SMTP provider's actual sender, host, port, and credentials. `security` supports verified `starttls` or implicit `tls`. Authentication may use `null` for both username and password reference when your TLS relay permits it. Secret references point to owner-only `0600` regular files. Preserve the generated queue-encryption key and set `public_origin` to the store's HTTPS origin. Restart the runner after editing the profile.

Delivery feedback distinguishes:

| Status | Meaning |
| --- | --- |
| `REQUEST_ACCEPTED` | The account action was accepted. |
| `queued` | Delivery is waiting for the worker. |
| `LOCAL_CAPTURED` | The private local SMTP inbox stored the email. |
| `SMTP_ACCEPTED` | The configured SMTP server accepted it; inbox delivery is unconfirmed. |
| `DELIVERY_FAILED` | An attempt failed or could not be confirmed; `retryable` indicates scheduled retry. |
| `CANCELED` | The token intent is no longer current. |
| `NOT_APPLICABLE` | The action does not require an email. |

Retries retain the same message identity. SMTP cannot guarantee exactly-once inbox delivery.

## Checks and maintenance

While the application is running, another activated terminal can run `python manage.py check --config /absolute/private/profile.json`. `check --declarations` validates configuration without database access. Local code checks are:

```sh
python -m pip install --require-hashes -r requirements-dev.lock
python -m pytest
npm run check
npm run test:contracts
```

Native tests require an explicitly configured, independently guarded test database and the `native` marker. They must use separate test state.

The initial worker grant lasts 30 days. Renew it through `rotate-workload --config ... --secret-input-file ...` using a protected manifest with `environment`, `merchant_id`, `machine_principal_id`, `service_id` (`sme-worker`), `credential_ref`, current `expected_revision` (initially `1`), and UTC `issued_at`/`expires_at` strings ending in `Z`. The lifetime is bounded to 90 days. The optional native runner does not automatically renew expired grants; the Docker supervisor performs the validated expiry-only renewal described above.

Encrypted backups require explicit `age`/`pg_dump` configuration. Restore/reset maintenance stages a fresh guarded replacement in `RECOVERY_HOLD`; reconciliation and reopening remain unavailable. Normal upgrades use `configure-mail`, `migrate`, build, and restart.

</details>
