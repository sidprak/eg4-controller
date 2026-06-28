# eg4-controller

Stateless cron script that **forbids on-grid battery discharge while the sun
is up** on an EG4 FlexBOSS21, so an Emporia EV charger in excess-solar mode
can't silently drain the home battery. Off-grid/EPS backup is **not**
affected — if the grid fails, the battery still powers your EPS loads down
to the off-grid SOC floor as before.

## Install

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> If your `pip` is configured against a private index that doesn't proxy
> PyPI, add `--index-url https://pypi.org/simple/`.

Copy and fill in the config:

```sh
cp .env.example .env
$EDITOR .env   # set EG4_USERNAME, EG4_PASSWORD, EG4_NORMAL_DISCHARGE_SOC, ...
```

## How the cap works

We control **`HOLD_DISCHG_CUT_OFF_SOC_EOD`** — the value the EG4 web UI
calls **"On-Grid Cut-Off SOC(%)"** (requires *Batt Discharge Control = SOC*).
While the grid is up, the battery serves on-grid loads only while SOC is
above this cutoff. Set it to `100` and on-grid discharge stops at any SOC:
loads above PV pull from grid instead of from the battery — so the Emporia
EV charger sees real grid flow, not a misleading "near-zero" reading from
silent battery backfill, and correctly throttles itself in excess-solar
mode. PV continues to serve loads normally, and off-grid/EPS backup is
untouched (separate `HOLD_SOC_LOW_LIMIT_EPS_DISCHG` floor).

| Zone | PV power | Action |
|---|---|---|
| Cap **ON** | `pv_w > EG4_PV_CAP_ON_W` (default 2000) | write `EG4_CAP_ON_SOC` (default `100`) — battery never discharges to on-grid loads |
| **Hold** | between cap-off and cap-on | leave current value alone (hysteresis — prevents flapping on spiky clouds) |
| Cap **OFF** | `pv_w < EG4_PV_CAP_OFF_W` (default 1500) | write `EG4_NORMAL_DISCHARGE_SOC` (default `2`) — normal behavior |

> **Rejected levers.** Writing `30000` to `HOLD_P_TO_USER_START_DISCHG`
> ("Start Discharge P_import(W)") is a no-op on FlexBOSS21 — the battery
> still feeds on-grid loads. `HOLD_DISCHG_POWER_PERCENT_CMD=0` would stop
> discharge but also disables EPS backup. Live testing confirmed
> `HOLD_DISCHG_CUT_OFF_SOC_EOD=100` blocks on-grid discharge (deficit
> served from grid) while PV→loads pass-through and EPS backup stay intact.

Crucially this does **not** touch `HOLD_SOC_LOW_LIMIT_EPS_DISCHG` (the
off-grid floor, currently `0`), so EPS/backup keeps full battery access.

## Confirm the hold-register key (one-time, read-only)

The defaults above are verified on **FlexBOSS21**. If you're on a different
EG4 / LuxPower model, confirm by dumping all settings:

```sh
set -a; source .env; set +a
.venv/bin/python guardrail.py --discover > discover.json
```

`discover.json` includes a `discharge_candidates` block and a `bank_status`
report (FlexBOSS21 firmware does not implement register banks 2000/5000 —
during normal runs the script skips them entirely to avoid the dongle
contention that causes `DEVICE_OFFLINE` storms; `--discover` still attempts
them so you can confirm your firmware's behavior).

The dump also lists runtime keys so you can confirm `EG4_PV_FIELD` (default
`ppv`) carries PV power in watts. Logs go to stderr so `> discover.json`
captures clean JSON.

## Dry-run rollout

`EG4_DRY_RUN=1` is the default — the script decides and logs but never
writes. Leave it on for at least a day and check the log decisions look
right:

```sh
.venv/bin/python guardrail.py
```

Each run emits one line shaped like:

```
decision=cap_on pv_w=1959.0 current=2 desired=100 action=would_write verify=skipped | {...json...}
```

## Cron (every 30 min)

Once you're happy with the dry-run output, flip `EG4_DRY_RUN=0` in `.env`
and install the cron entry:

```cron
*/30 * * * * cd /path/to/eg4-controller && \
  set -a && . ./.env && set +a && \
  .venv/bin/python guardrail.py >> /var/log/eg4-guardrail.log 2>&1
```

For a hands-off cloud deployment, see [`lambda/README.md`](./lambda/README.md)
— AWS Lambda + EventBridge cron, free within the AWS free tier.

## Modes

| Flag           | Behavior                                                  |
|----------------|-----------------------------------------------------------|
| (none)         | Honors `EG4_DRY_RUN` (default `1` = no writes).           |
| `--dry-run`    | Forces dry-run even if `EG4_DRY_RUN=0`.                   |
| `--apply`      | Forces real writes even if `EG4_DRY_RUN=1`.               |
| `--discover`   | Read-only dump of all hold-register keys + runtime keys.  |

`--list-settings` is accepted as an alias for `--discover`.

## Safety properties

- **Idempotent.** Reads the current threshold and only writes if it differs
  from the desired value.
- **Verified.** After every write, re-reads the hold register and logs
  `verify=pass|fail` based on whether the value stuck.
- **Retry-on-transient.** EG4 cloud occasionally returns `DEVICE_OFFLINE`
  for a couple of minutes after a burst of register reads. Writes and
  verify-reads retry once after a ~90 s / ~45 s backoff so a single
  transient blip doesn't fail the run.
- **Off-grid preserved.** We never touch the EPS discharge floor. If your
  grid fails mid-cap, the battery still powers EPS loads.
- **Fail-safe.** Any error (auth, telemetry, missing hold-param, write
  rejection) logs and exits non-zero **without** writing a guessed value.
- **Re-authenticates every run.** No persistent state between cron
  invocations; the library also retries once on HTTP 401.

## Config reference

See [`.env.example`](./.env.example) for every variable with defaults.
