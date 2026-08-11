# eg4-controller

Stateless cron script that **forbids on-grid battery discharge while the EV
charger is drawing excess solar** on an EG4 FlexBOSS21 + GridBoss, so an
Emporia EV charger in excess-solar mode can't silently drain the home
battery. The cap engages **only while the EV is actively charging** (read
from its GridBoss smart port) — so normal household loads still draw from the
battery as usual. Off-grid/EPS backup is **not** affected — if the grid
fails, the battery still powers your EPS loads down to the off-grid SOC floor
as before.

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
$EDITOR .env   # set EG4_USERNAME, EG4_PASSWORD, EG4_EV_SMART_PORT, ...
```

## How the cap works

**Trigger — the EV charger's GridBoss smart port.** Each run reads the
GridBoss (MidBox) runtime and sums the EV port's L1+L2 active power. The
Emporia only excess-solar charges at ≥7 A @ 240 V (~1680 W) or not at all, so
the port reads ~0 W (idle) or ≥1680 W (charging) with a clean gap between.

**Lever — `HOLD_DISCHG_CUT_OFF_SOC_EOD`**, the value the EG4 web UI calls
**"On-Grid Cut-Off SOC(%)"** (requires *Batt Discharge Control = SOC*). While
the grid is up, the battery serves on-grid loads only while SOC is above this
cutoff. Set it to `100` and on-grid discharge stops at any SOC: the EV's load
above PV pulls from grid instead of from the battery — so the Emporia sees
real grid flow, not a misleading "near-zero" reading from silent battery
backfill, and correctly throttles itself. Off-grid/EPS backup is untouched
(separate `HOLD_SOC_LOW_LIMIT_EPS_DISCHG` floor).

| Zone | EV smart-port power | Action |
|---|---|---|
| Cap **ON** | `ev_w > EG4_EV_CAP_ON_W` (default 1500) | write `EG4_CAP_ON_SOC` (default `100`) — battery won't backfill the EV |
| **Hold** | between cap-off and cap-on | leave current value alone (hysteresis around the ~1680 W boundary) |
| Cap **OFF** | `ev_w < EG4_EV_CAP_OFF_W` (default 1000) | write `EG4_NORMAL_DISCHARGE_SOC` (default `2`) — normal battery behavior |

> **Why trigger on EV load, not PV?** An earlier version keyed the cap on PV
> power. That caused two problems: (1) with cutoff=100 at low SOC the inverter
> enters "end-of-discharge → grid bypass", running the house on grid while PV
> charges the battery; and (2) normal evening loads (dishwasher/laundry) at
> PV~2 kW got pushed to grid instead of the battery. Keying on the EV smart
> port confines the cap to exactly the situation it's meant for.

> **Rejected levers.** Writing `30000` to `HOLD_P_TO_USER_START_DISCHG`
> ("Start Discharge P_import(W)") is a no-op on FlexBOSS21 — the battery
> still feeds on-grid loads. `HOLD_DISCHG_POWER_PERCENT_CMD=0` would stop
> discharge but also disables EPS backup. Live testing confirmed
> `HOLD_DISCHG_CUT_OFF_SOC_EOD=100` blocks on-grid discharge (deficit
> served from grid) while PV→loads pass-through and EPS backup stay intact.

Crucially this does **not** touch `HOLD_SOC_LOW_LIMIT_EPS_DISCHG` (the
off-grid floor, currently `0`), so EPS/backup keeps full battery access.

## Cloudy-day pre-peak top-up

The optional top-up controller fills the battery from grid before peak hours,
then returns the inverter to self-consumption as soon as the target is reached.
It is disabled by default.

First configure the inverter's native **AC Charge** schedule with one window
from **14:00 to 15:00**, select its time-based / **According to Time** mode,
and disable its other AC Charge windows. This is required so arming the switch
at 13:30 cannot start charging early. Leave Battery Charge Control in SOC mode.
Then enable:

```sh
EG4_TOP_UP_ENABLED=1
EG4_TOP_UP_TIMEZONE=America/Los_Angeles
EG4_TOP_UP_START=14:00
EG4_TOP_UP_END=15:00
EG4_TOP_UP_TARGET_SOC=65
```

The controller uses the native schedule as a safety boundary:

| Local time / state | Action |
|---|---|
| 13:30–14:00, SOC below 65% | Set the AC Charge SOC limit to 65% and arm AC Charge |
| 13:30–14:00, SOC at/above 65% | Keep AC Charge off; no grid top-up is needed |
| 14:00–15:00, AC Charge armed | Keep charging until SOC reaches 65% |
| SOC reaches 65% | Disable AC Charge immediately, restoring PV→house and excess PV→battery |
| AC Charge already off during the window | Keep it off, even if SOC later dips; this prevents restart cycling |
| Outside the arm/window period | Keep AC Charge off |

With the default 30-minute cadence, AC Charge is disabled on the next
half-hour invocation after the target is reached. The extra off-peak charging
is harmless if the battery reaches 65% between runs.
`EG4_HOLD_PARAM_AC_CHARGE=FUNC_AC_CHARGE` and
`EG4_HOLD_PARAM_AC_CHARGE_SOC=HOLD_AC_CHARGE_SOC_LIMIT` are the FlexBOSS21
cloud keys; confirm them with `--discover` before enabling this on another
model.

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

It also includes a `gridboss_serial` and a `gridboss_smart_ports` block with
each port's status and active power — use these to confirm `EG4_GRIDBOSS_SERIAL`
and pick the `EG4_EV_SMART_PORT` your EV charger is wired to (plug it in and
watch which port's `active_power_w` rises). The dump lists runtime keys too so
you can confirm `EG4_PV_FIELD` (default `ppv`) carries PV power in watts (used
for log context only). Logs go to stderr so `> discover.json` captures clean
JSON.

## Dry-run rollout

`EG4_DRY_RUN=1` is the default — the script decides and logs but never
writes. Leave it on for at least a day and check the log decisions look
right:

```sh
.venv/bin/python guardrail.py
```

Each run emits one line shaped like:

```
decision=cap_on ev_w=6480.0 current=2 desired=100 action=would_write verify=skipped | {...json...}
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
