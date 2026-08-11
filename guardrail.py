#!/usr/bin/env python3
"""EG4 battery discharge guardrail and optional pre-peak top-up.

Stateless script intended for cron. Each run:
  authenticate → read telemetry → decide desired controls →
  read current settings → write only differences → verify by re-read →
  emit one structured JSON log line.

See README.md for usage; the design rationale is summarized there.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from datetime import datetime, time, timedelta, timezone
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from eg4_inverter_api import EG4InverterAPI
from eg4_inverter_api.constants import INVERTER_PARAMETER_READ, INVERTER_PARAMETER_WRITE
from eg4_inverter_api.exceptions import EG4APIError, EG4AuthError

# Hold-register banks that contain real data on FlexBOSS21 firmware. Banks
# 2000 and 5000 are documented in EG4's cloud spec but consistently return
# DEVICE_OFFLINE / DATAFRAME_TIMEOUT here, and the extra round-trips cost
# ~5-10 s of dongle time per run. That dongle contention is what tips the
# cloud into thinking the inverter is offline during the subsequent write.
# Skipping the dead banks dramatically reduces transient write failures.
# Override with EG4_SETTING_BANKS=0,127,240,500,2000,5000 if your firmware
# populates the other banks.
DEFAULT_SETTING_BANKS = (0, 127, 240, 500)

# Full bank sweep used by --discover so users on other firmware can see
# what their device actually returns.
DISCOVER_SETTING_BANKS = (0, 127, 240, 500, 2000, 5000)

# EG4 cloud msgCodes that mean "the dongle is momentarily unreachable; try
# again in ~60-120 s". DEVICE_OFFLINE in particular shows up after a burst
# of reads even when the inverter is plainly online.
TRANSIENT_MSG_CODES = frozenset({1002, 1003})  # DATAFRAME_TIMEOUT, DEVICE_OFFLINE

# Retry budgets sized so a single retry covers EG4's typical ~60-120 s
# DEVICE_OFFLINE window. Each script run takes ~30 s of EG4-cloud latency
# on top of these sleeps, so the Lambda Timeout must accommodate the worst
# case (~30 + 90 + 5 + 45 + 5 ≈ 175 s on a fully-retried run).
WRITE_MAX_ATTEMPTS = 2
WRITE_RETRY_BACKOFF_S = 90
READ_MAX_ATTEMPTS = 2
READ_RETRY_BACKOFF_S = 45

# Default hold-register key. UI label: "On-Grid Cut-Off SOC(%)".
# Meaning: while the grid is present, the battery discharges to on-grid
# loads only while SOC > this cutoff; at/below it, on-grid discharge stops
# and the grid covers any load deficit (PV still serves loads normally).
# So setting the cutoff to 100 forbids on-grid battery discharge at any
# SOC, while leaving off-grid/EPS discharge — governed by the separate
# HOLD_SOC_LOW_LIMIT_EPS_DISCHG floor — fully available in an outage.
#
# Two earlier levers were tried and rejected: HOLD_P_TO_USER_START_DISCHG
# (30000) is a no-op on FlexBOSS21 — the battery still feeds on-grid loads;
# HOLD_DISCHG_POWER_PERCENT_CMD=0 also disables EPS backup. Live testing
# confirmed cutoff=100 blocks on-grid discharge (deficit served from grid)
# while PV->loads pass-through and EPS backup remain intact.
DEFAULT_HOLD_PARAM_DISCHARGE = "HOLD_DISCHG_CUT_OFF_SOC_EOD"

# Default SOC (%) cutoff written when the cap is ON. 100 stops on-grid
# discharge at any SOC; loads above PV pull from grid instead of battery.
DEFAULT_CAP_ON_SOC = "100"

# Default SOC (%) cutoff restored when the cap is OFF. 2 matches the
# typical FlexBOSS21 setting; set whatever shows in the EG4 web UI as
# "On-Grid Cut-Off SOC(%)" today. Requires Batt Discharge Control = SOC.
DEFAULT_NORMAL_SOC = "2"

# Cutoff is an SOC percentage; out-of-range values are config typos.
SOC_MIN = 0
SOC_MAX = 100

# GridBoss (MidBox) smart-port EV trigger. The cap engages only while the EV
# charger on this smart port is actively drawing power (excess-solar
# charging), so normal household loads and idle periods never trip it. We read
# per-port active power (W) from the midbox runtime endpoint and sum L1+L2.
MIDBOX_RUNTIME_ENDPOINT = "/WManage/api/midbox/getMidboxRuntime"
FUNCTION_CONTROL_ENDPOINT = "/WManage/web/maintain/remoteSet/functionControl"

# GridBoss smart port the EV charger is wired to (1-4). UI: "Smart Port N".
DEFAULT_EV_SMART_PORT = "1"

# EV-load hysteresis (watts). The Emporia only excess-solar charges at >=7 A
# @ 240 V (~1680 W) or not at all, so smart-port power is cleanly ~0 (idle)
# or >=1680 (charging). Cap ON above CAP_ON_W, OFF below CAP_OFF_W; between,
# the previous state is held (prevents flapping at the charging boundary).
DEFAULT_EV_CAP_ON_W = 1500
DEFAULT_EV_CAP_OFF_W = 1000

# Optional cloudy-day grid top-up. The inverter's native AC-charge schedule
# remains authoritative; this controller arms the mode shortly before that
# window, then disables it as soon as the target SOC is reached.
DEFAULT_TOP_UP_TIMEZONE = "America/Los_Angeles"
DEFAULT_TOP_UP_START = "14:00"
DEFAULT_TOP_UP_END = "15:00"
DEFAULT_TOP_UP_ARM_MINUTES = 30
DEFAULT_TOP_UP_TARGET_SOC = 65
DEFAULT_HOLD_PARAM_AC_CHARGE = "FUNC_AC_CHARGE"
DEFAULT_HOLD_PARAM_AC_CHARGE_SOC = "HOLD_AC_CHARGE_SOC_LIMIT"


def _env_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return int(raw)


def _parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="EG4 battery discharge guardrail")
    p.add_argument(
        "--discover",
        "--list-settings",
        action="store_true",
        dest="discover",
        help="Read-only: dump all hold-register keys+values and exit. "
             "Use this to identify EG4_HOLD_PARAM_DISCHARGE.",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Decide and log, never write. Overrides EG4_DRY_RUN=0.",
    )
    p.add_argument(
        "--apply",
        action="store_true",
        help="Force write mode even if EG4_DRY_RUN=1.",
    )
    return p.parse_args()


def _emit(record: dict[str, Any]) -> None:
    """Emit a single structured log line: human prefix + JSON blob."""
    record.setdefault("ts", datetime.now(timezone.utc).isoformat())
    decision = record.get("decision", "?")
    action = record.get("action", "?")
    verify = record.get("verify", "?")
    ev_w = record.get("ev_w")
    current = record.get("current_value")
    desired = record.get("desired_value")
    summary = (
        f"decision={decision} ev_w={ev_w} current={current} "
        f"desired={desired} action={action} verify={verify}"
    )
    line = f"{summary} | {json.dumps(record, default=str, sort_keys=True)}"
    logging.info(line)


def _setup_logging() -> None:
    # logs go to stderr so --discover's stdout JSON stays clean for piping
    handlers: list[logging.Handler] = [logging.StreamHandler(sys.stderr)]
    log_file = os.getenv("EG4_LOG_FILE")
    if log_file:
        handlers.append(logging.FileHandler(log_file))
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=handlers,
        force=True,
    )


def _extract_pv_w(rt: Any, field: str) -> float | None:
    """Pull PV power (W) from a RuntimeData object using `field` (default `ppv`).

    Returns None if missing/unparseable — caller treats that as fail-safe.
    """
    val = getattr(rt, field, None)
    if val is None:
        # Last-resort: sum any pv1..pv4 power fields if present.
        parts = [getattr(rt, f"ppv{i}", None) for i in (1, 2, 3, 4)]
        nums = [float(p) for p in parts if p not in (None, "")]
        if not nums:
            return None
        return sum(nums)
    try:
        return float(val)
    except (TypeError, ValueError):
        return None


def _resolve_gridboss_serial(api: EG4InverterAPI) -> str | None:
    """Return the GridBoss (MidBox) serial to read smart-port power from.

    Honors EG4_GRIDBOSS_SERIAL if set; otherwise auto-detects the batteryless
    device on the account (the GridBoss reports batteryType=NO_BATTERY /
    deviceType=9, distinct from the FlexBOSS inverter).
    """
    explicit = os.getenv("EG4_GRIDBOSS_SERIAL")
    if explicit and explicit.strip():
        return explicit.strip()
    try:
        inverters = api.get_inverters()
    except Exception:  # noqa: BLE001 — caller treats None as fail-safe
        return None
    for inv in inverters:
        if getattr(inv, "batteryType", None) == "NO_BATTERY" or \
                getattr(inv, "deviceType", None) == 9:
            sn = getattr(inv, "serialNum", None)
            if sn:
                return str(sn)
    return None


async def _read_ev_power(
    api: EG4InverterAPI, gridboss_serial: str, smart_port: int,
) -> tuple[float | None, str | None]:
    """Return (EV active power in W, error). Sums smart-port L1+L2 active power.

    Reads the GridBoss midbox runtime, retrying transient failures. Returns
    (None, reason) if the port power can't be read so the caller fails safe
    without writing a guessed cap state.
    """
    url = f"{api._base_url}{MIDBOX_RUNTIME_ENDPOINT}"
    payload = f"serialNum={gridboss_serial}"
    last_err: str | None = None
    for attempt in range(1, READ_MAX_ATTEMPTS + 1):
        try:
            resp = await api._request("POST", url, payload)
        except Exception as e:  # noqa: BLE001 — treat as transient, retry
            last_err = f"{type(e).__name__}: {e}"
            resp = None
        if resp is not None:
            if not resp.get("success"):
                last_err = f"midbox read success=false: msg={resp.get('msg')}"
            else:
                m = resp.get("midboxData") or {}
                l1 = m.get(f"smartLoad{smart_port}L1ActivePower")
                l2 = m.get(f"smartLoad{smart_port}L2ActivePower")
                if l1 is None and l2 is None:
                    last_err = (f"smartLoad{smart_port} active power missing "
                                "from midboxData")
                else:
                    try:
                        return float(l1 or 0) + float(l2 or 0), None
                    except (TypeError, ValueError):
                        last_err = (f"unparseable smart-port power: "
                                    f"{l1!r}+{l2!r}")
        if attempt < READ_MAX_ATTEMPTS:
            await asyncio.sleep(READ_RETRY_BACKOFF_S)
    return None, last_err


def _extract_setting_value(settings: dict[str, Any], hold_param: str) -> str | None:
    """Locate the hold-register value in the merged settings dict.

    EG4 settings appear under their hold-register names
    (e.g. HOLD_DISCHG_POWER_PERCENT_CMD).
    """
    val = settings.get(hold_param)
    if val is None:
        return None
    # Some EG4 responses wrap values in dicts; flatten common shapes.
    if isinstance(val, dict):
        for key in ("valueText", "value", "text"):
            if key in val:
                return str(val[key])
        return json.dumps(val, sort_keys=True)
    return str(val)


async def _read_settings_tolerant(
    api: EG4InverterAPI,
    banks: tuple[int, ...] = DEFAULT_SETTING_BANKS,
) -> tuple[dict[str, Any], dict[int, dict[str, Any]]]:
    """Read every requested hold-register bank, tolerating per-bank failures.

    Returns (merged_fields, per_bank_status). The lib's read_settings_async
    bails on the first non-success and discards everything already collected;
    FlexBOSS21 firmware returns success=false for some banks, so we iterate
    ourselves and merge what we can. The status dict carries `msg`/`msgCode`
    so callers can distinguish transient (DEVICE_OFFLINE) from permanent
    errors.
    """
    url = f"{api._base_url}{INVERTER_PARAMETER_READ}"
    merged: dict[str, Any] = {}
    status: dict[int, dict[str, Any]] = {}
    skip = {"success", "valueFrame", "inverterSn", "startRegister",
            "pointNumber", "error", "msg", "msgCode", "extData"}
    for start in banks:
        payload = (f"inverterSn={api._serialNum}&startRegister={start}"
                   f"&pointNumber=127&autoRetry=true")
        try:
            resp = await api._request("POST", url, payload)
        except Exception as e:  # noqa: BLE001 — record per-bank, keep going
            status[start] = {"success": False, "error": f"{type(e).__name__}: {e}",
                             "msg": None, "msgCode": None, "n_fields": 0}
            continue
        ok = bool(resp.get("success"))
        extras = {k: v for k, v in resp.items() if k not in skip}
        status[start] = {"success": ok, "error": resp.get("error"),
                         "msg": resp.get("msg"), "msgCode": resp.get("msgCode"),
                         "n_fields": len(extras)}
        if ok:
            merged.update(extras)
    return merged, status


def _is_transient_response(resp: dict[str, Any]) -> bool:
    """True if an EG4 cloud response indicates a temporary dongle hiccup."""
    if resp.get("success"):
        return False
    code = resp.get("msgCode")
    if code in TRANSIENT_MSG_CODES:
        return True
    msg = (resp.get("msg") or "").upper()
    return "OFFLINE" in msg or "TIMEOUT" in msg


def _bank_status_has_transient(status: dict[int, dict[str, Any]]) -> bool:
    return any(
        not s["success"] and (
            s.get("msgCode") in TRANSIENT_MSG_CODES
            or "OFFLINE" in (s.get("msg") or "").upper()
            or "TIMEOUT" in (s.get("msg") or "").upper()
        )
        for s in status.values()
    )


def _parse_setting_banks(raw: str | None) -> tuple[int, ...]:
    if not raw or not raw.strip():
        return DEFAULT_SETTING_BANKS
    parts = [p.strip() for p in raw.split(",") if p.strip()]
    return tuple(int(p) for p in parts)


async def _read_hold_params_with_retry(
    api: EG4InverterAPI,
    hold_params: tuple[str, ...],
    banks: tuple[int, ...],
) -> tuple[dict[str, str], dict[int, dict[str, Any]], int]:
    """Read hold params via one bank sweep, retrying missing/transient values.

    Returns (found_values, last_bank_status, attempts). The EG4 cloud
    sometimes returns success=true for a bank but omits individual keys, so
    we retry on "value not found" as well as on DEVICE_OFFLINE storms.
    """
    last_status: dict[int, dict[str, Any]] = {}
    for attempt in range(1, READ_MAX_ATTEMPTS + 1):
        settings, last_status = await _read_settings_tolerant(api, banks)
        values = {
            param: value
            for param in hold_params
            if (value := _extract_setting_value(settings, param)) is not None
        }
        missing = [param for param in hold_params if param not in values]
        if not missing:
            return values, last_status, attempt
        if attempt < READ_MAX_ATTEMPTS:
            reason = ("transient cloud error"
                      if _bank_status_has_transient(last_status)
                      else f"keys missing: {', '.join(missing)}")
            logging.info(
                "settings read attempt %d/%d: %s; sleeping %ds and retrying",
                attempt, READ_MAX_ATTEMPTS, reason, READ_RETRY_BACKOFF_S,
            )
            await asyncio.sleep(READ_RETRY_BACKOFF_S)
    return values, last_status, READ_MAX_ATTEMPTS


async def _read_hold_param_with_retry(
    api: EG4InverterAPI,
    hold_param: str,
    banks: tuple[int, ...],
) -> tuple[str | None, dict[int, dict[str, Any]], int]:
    values, status, attempts = await _read_hold_params_with_retry(
        api, (hold_param,), banks,
    )
    return values.get(hold_param), status, attempts


def _parse_hhmm(raw: str, name: str) -> time:
    try:
        return datetime.strptime(raw, "%H:%M").time()
    except ValueError as e:
        raise ValueError(f"{name} must use 24-hour HH:MM, got {raw!r}") from e


def _parse_bool_setting(raw: str) -> bool:
    normalized = raw.strip().lower()
    if normalized in {"1", "true", "on", "enabled", "enable"}:
        return True
    if normalized in {"0", "false", "off", "disabled", "disable"}:
        return False
    raise ValueError(f"unrecognized boolean setting value {raw!r}")


def _top_up_decision(
    now: datetime,
    start: time,
    end: time,
    arm_minutes: int,
    target_soc: int,
    soc: float | None,
    ac_charge_enabled: bool,
) -> tuple[str, bool]:
    """Return the top-up phase and desired AC-charge enable state."""
    start_at = datetime.combine(now.date(), start, tzinfo=now.tzinfo)
    end_at = datetime.combine(now.date(), end, tzinfo=now.tzinfo)
    if end_at <= start_at:
        raise ValueError("top-up window must start and end on the same day")
    arm_at = start_at - timedelta(minutes=arm_minutes)

    if arm_at <= now < start_at:
        if soc is None:
            return "telemetry_unavailable", False
        return ("arm" if soc < target_soc else "not_needed"), soc < target_soc
    if start_at <= now < end_at:
        if not ac_charge_enabled:
            return "complete", False
        if soc is None:
            return "telemetry_unavailable", False
        if soc >= target_soc:
            return "target_reached", False
        return "charging", True
    return "standby", False


async def _write_hold_param_with_retry(
    api: EG4InverterAPI,
    hold_param: str,
    value_text: str,
) -> tuple[bool, dict[str, Any], int]:
    """Write a hold register or function switch, retrying transient errors.

    Returns (success, last_response, attempts). We bypass the library's
    write_setting_async because it discards the response body; we need the
    msgCode to decide whether retrying is worthwhile.
    """
    if hold_param.startswith("FUNC_"):
        url = f"{api._base_url}{FUNCTION_CONTROL_ENDPOINT}"
        enabled = _parse_bool_setting(value_text)
        payload = (
            f"inverterSn={api._serialNum}&functionParam={hold_param}"
            f"&enable={'true' if enabled else 'false'}"
            "&clientType=WEB&remoteSetType=NORMAL"
        )
    else:
        url = f"{api._base_url}{INVERTER_PARAMETER_WRITE}"
        payload = (f"inverterSn={api._serialNum}&holdParam={hold_param}"
                   f"&valueText={value_text}&clientType=WEB&remoteSetType=NORMAL")
    last: dict[str, Any] = {}
    for attempt in range(1, WRITE_MAX_ATTEMPTS + 1):
        try:
            last = await api._request("POST", url, payload)
        except Exception as e:  # noqa: BLE001 — treat exceptions as transient
            last = {"success": False, "error": f"{type(e).__name__}: {e}",
                    "msg": "exception", "msgCode": None}
        if last.get("success"):
            return True, last, attempt
        if attempt == WRITE_MAX_ATTEMPTS or not _is_transient_response(last):
            return False, last, attempt
        logging.info(
            "write attempt %d/%d returned transient error msg=%s msgCode=%s; "
            "sleeping %ds and retrying",
            attempt, WRITE_MAX_ATTEMPTS,
            last.get("msg"), last.get("msgCode"), WRITE_RETRY_BACKOFF_S,
        )
        await asyncio.sleep(WRITE_RETRY_BACKOFF_S)
    return False, last, WRITE_MAX_ATTEMPTS


async def _discover(api: EG4InverterAPI) -> int:
    settings, bank_status = await _read_settings_tolerant(
        api, banks=DISCOVER_SETTING_BANKS,
    )
    rt = await api.get_inverter_runtime_async()
    if hasattr(rt, "success") and rt.success is False:
        logging.error("runtime read failed: %r", rt)
        return 2
    rt_dict = {k: v for k, v in (rt.to_dict() if hasattr(rt, "to_dict") else {}).items()
               if not k.startswith("_")}
    # Pull a few keys that are useful for picking a hold_param and PV field.
    discharge_candidates = {
        k: v for k, v in settings.items()
        if "DISCH" in k or "SOC" in k or "EOD" in k
    }
    top_up_candidates = {
        k: v for k, v in settings.items()
        if "AC_CHARGE" in k
    }
    # GridBoss smart-port sample so users can confirm EG4_EV_SMART_PORT and
    # EG4_GRIDBOSS_SERIAL against the port their EV charger is wired to.
    gridboss_serial = _resolve_gridboss_serial(api)
    gridboss_smart_ports: dict[str, Any] = {}
    if gridboss_serial:
        try:
            resp = await api._request(
                "POST", f"{api._base_url}{MIDBOX_RUNTIME_ENDPOINT}",
                f"serialNum={gridboss_serial}",
            )
            m = resp.get("midboxData") or {}
            for p in (1, 2, 3, 4):
                gridboss_smart_ports[f"port{p}"] = {
                    "status": m.get(f"smartPort{p}Status"),
                    "active_power_w": (m.get(f"smartLoad{p}L1ActivePower") or 0)
                    + (m.get(f"smartLoad{p}L2ActivePower") or 0),
                }
        except Exception as e:  # noqa: BLE001 — best-effort diagnostic
            gridboss_smart_ports = {"error": f"{type(e).__name__}: {e}"}
    print(json.dumps(
        {
            "serial": api._serialNum,
            "gridboss_serial": gridboss_serial,
            "gridboss_smart_ports": gridboss_smart_ports,
            "bank_status": bank_status,
            "settings": settings,
            "discharge_candidates": discharge_candidates,
            "top_up_candidates": top_up_candidates,
            "runtime_keys": sorted(rt_dict.keys()),
            "runtime_pv_sample": {
                k: rt_dict.get(k)
                for k in ("ppv", "ppv1", "ppv2", "ppv3", "ppv4",
                          "pDisCharge", "pCharge", "pToGrid", "pToUser")
            },
        },
        indent=2,
        default=str,
        sort_keys=True,
    ))
    return 0


async def _run(args: argparse.Namespace) -> int:
    username = os.getenv("EG4_USERNAME")
    password = os.getenv("EG4_PASSWORD")
    if not username or not password:
        logging.error("EG4_USERNAME and EG4_PASSWORD are required")
        return 2

    base_url = os.getenv("EG4_BASE_URL", "https://monitor.eg4electronics.com")
    ignore_ssl = _env_bool("EG4_DISABLE_VERIFY_SSL", False)
    serial = os.getenv("EG4_SERIAL_NUMBER")

    api = EG4InverterAPI(username, password, base_url=base_url)
    try:
        await api.login(ignore_ssl=ignore_ssl)
        if serial:
            api.set_selected_inverter(serialNum=serial)
        else:
            api.set_selected_inverter(inverterIndex=0)

        if args.discover:
            return await _discover(api)

        return await _decide_and_write(api, args)
    except EG4AuthError as e:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"auth: {e}"})
        return 2
    except EG4APIError as e:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"api: {e}"})
        return 2
    except Exception as e:  # noqa: BLE001 — fail-safe top-level guard
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"unexpected: {type(e).__name__}: {e}"})
        return 2
    finally:
        await api.close()


async def _decide_and_write(api: EG4InverterAPI, args: argparse.Namespace) -> int:
    # EV-load hysteresis thresholds (watts on the GridBoss smart port). The cap
    # engages only while the EV charger is actively drawing power (excess-solar
    # charging), so normal household loads and idle periods never trip it. Two
    # separate cutoffs give hysteresis around the ~1680 W charging boundary.
    ev_cap_on_w = _env_int("EG4_EV_CAP_ON_W", DEFAULT_EV_CAP_ON_W)
    ev_cap_off_w = _env_int("EG4_EV_CAP_OFF_W", DEFAULT_EV_CAP_OFF_W)
    smart_port = _env_int("EG4_EV_SMART_PORT", int(DEFAULT_EV_SMART_PORT))
    # Hold-register SOC cutoff values (%).
    normal_threshold = os.getenv("EG4_NORMAL_DISCHARGE_SOC", DEFAULT_NORMAL_SOC)
    cap_on_threshold = os.getenv("EG4_CAP_ON_SOC", DEFAULT_CAP_ON_SOC)
    hold_param = os.getenv("EG4_HOLD_PARAM_DISCHARGE", DEFAULT_HOLD_PARAM_DISCHARGE)
    pv_field = os.getenv("EG4_PV_FIELD", "ppv")
    top_up_enabled = _env_bool("EG4_TOP_UP_ENABLED", False)
    top_up_hold_param = os.getenv(
        "EG4_HOLD_PARAM_AC_CHARGE", DEFAULT_HOLD_PARAM_AC_CHARGE,
    )
    top_up_soc_hold_param = os.getenv(
        "EG4_HOLD_PARAM_AC_CHARGE_SOC", DEFAULT_HOLD_PARAM_AC_CHARGE_SOC,
    )

    if ev_cap_on_w < ev_cap_off_w:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_EV_CAP_ON_W ({ev_cap_on_w}) must be >= "
                        f"EG4_EV_CAP_OFF_W ({ev_cap_off_w}); otherwise "
                        "hysteresis logic is inverted"})
        return 2

    try:
        normal_int = int(float(normal_threshold))
    except ValueError:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_NORMAL_DISCHARGE_SOC not numeric: "
                        f"{normal_threshold!r}"})
        return 2
    try:
        cap_on_int = int(float(cap_on_threshold))
    except ValueError:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_SOC not numeric: {cap_on_threshold!r}"})
        return 2
    if not SOC_MIN <= normal_int <= SOC_MAX:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_NORMAL_DISCHARGE_SOC must be "
                        f"{SOC_MIN}..{SOC_MAX}, got {normal_int}"})
        return 2
    if not SOC_MIN <= cap_on_int <= SOC_MAX:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_SOC must be "
                        f"{SOC_MIN}..{SOC_MAX}, got {cap_on_int}"})
        return 2
    if cap_on_int <= normal_int:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_SOC ({cap_on_int}) must be > "
                        f"EG4_NORMAL_DISCHARGE_SOC ({normal_int}); "
                        "otherwise cap-on would not raise the cutoff"})
        return 2

    top_up_config: dict[str, Any] | None = None
    if top_up_enabled:
        try:
            top_up_target_soc = _env_int(
                "EG4_TOP_UP_TARGET_SOC", DEFAULT_TOP_UP_TARGET_SOC,
            )
            top_up_arm_minutes = _env_int(
                "EG4_TOP_UP_ARM_MINUTES", DEFAULT_TOP_UP_ARM_MINUTES,
            )
            top_up_start = _parse_hhmm(
                os.getenv("EG4_TOP_UP_START", DEFAULT_TOP_UP_START),
                "EG4_TOP_UP_START",
            )
            top_up_end = _parse_hhmm(
                os.getenv("EG4_TOP_UP_END", DEFAULT_TOP_UP_END),
                "EG4_TOP_UP_END",
            )
            top_up_timezone_name = os.getenv(
                "EG4_TOP_UP_TIMEZONE", DEFAULT_TOP_UP_TIMEZONE,
            )
            top_up_timezone = ZoneInfo(top_up_timezone_name)
            if not SOC_MIN <= top_up_target_soc <= SOC_MAX:
                raise ValueError(
                    f"EG4_TOP_UP_TARGET_SOC must be {SOC_MIN}..{SOC_MAX}, "
                    f"got {top_up_target_soc}"
                )
            if not 1 <= top_up_arm_minutes <= 180:
                raise ValueError(
                    "EG4_TOP_UP_ARM_MINUTES must be 1..180, "
                    f"got {top_up_arm_minutes}"
                )
            # Validate the same-day window before making any API calls.
            probe_now = datetime.now(top_up_timezone)
            _top_up_decision(
                probe_now, top_up_start, top_up_end, top_up_arm_minutes,
                top_up_target_soc, 0.0, False,
            )
            top_up_config = {
                "target_soc": top_up_target_soc,
                "arm_minutes": top_up_arm_minutes,
                "start": top_up_start,
                "end": top_up_end,
                "timezone": top_up_timezone,
                "timezone_name": top_up_timezone_name,
            }
        except (ValueError, ZoneInfoNotFoundError) as e:
            _emit({"decision": "error", "action": "none", "verify": "skipped",
                   "error": f"top-up configuration: {e}"})
            return 2

    dry_run = _env_bool("EG4_DRY_RUN", True)
    if args.dry_run:
        dry_run = True
    if args.apply:
        dry_run = False

    # Resolve the GridBoss and read the EV charger's smart-port power — this is
    # the decision input, so a failure fails safe (no write).
    gridboss_serial = _resolve_gridboss_serial(api)
    if not gridboss_serial:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": "could not resolve GridBoss serial; set "
                        "EG4_GRIDBOSS_SERIAL to the MidBox serial number"})
        return 2

    ev_w, ev_err = await _read_ev_power(api, gridboss_serial, smart_port)
    if ev_w is None:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "gridboss_serial": gridboss_serial, "smart_port": smart_port,
               "error": f"EV smart-port power read failed: {ev_err}"})
        return 2

    # Runtime is best-effort context for the EV guardrail, but SOC is required
    # while the optional top-up is arming or active.
    pv_w = None
    soc: float | None = None
    try:
        rt = await api.get_inverter_runtime_async()
        if not (hasattr(rt, "success") and rt.success is False):
            pv_w = _extract_pv_w(rt, pv_field)
            soc_raw = getattr(rt, "soc", None)
            if soc_raw not in (None, ""):
                soc = float(soc_raw)
    except Exception:  # noqa: BLE001 — context only, safe to ignore
        pass

    # Read all controlled settings in one sweep to minimize EG4 dongle load.
    setting_banks = _parse_setting_banks(os.getenv("EG4_SETTING_BANKS"))
    required_params = [hold_param]
    if top_up_enabled:
        required_params.extend((top_up_hold_param, top_up_soc_hold_param))
    current_values, bank_status, read_attempts = await _read_hold_params_with_retry(
        api, tuple(required_params), setting_banks,
    )
    missing_params = [
        param for param in required_params if param not in current_values
    ]
    if missing_params:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "ev_w": ev_w, "hold_param": hold_param,
               "bank_status": bank_status, "read_attempts": read_attempts,
               "error": f"hold params not found after {read_attempts} "
                        f"settings-read attempts: {', '.join(missing_params)}; "
                        "run --discover to confirm key / firmware health"})
        return 2
    current_raw = current_values[hold_param]

    # Common context included in every decision log line.
    ctx = {
        "ev_w": ev_w, "ev_cap_on_w": ev_cap_on_w, "ev_cap_off_w": ev_cap_off_w,
        "smart_port": smart_port, "gridboss_serial": gridboss_serial,
        "pv_w": pv_w, "soc": soc, "hold_param": hold_param,
    }

    # Three-zone hysteresis on EV smart-port power:
    #   ev_w > cap_on_w   → cap_on  (EV excess-solar charging; block discharge)
    #   ev_w < cap_off_w  → cap_off (EV idle; normal battery behavior)
    #   between           → hold    (leave current value alone)
    if ev_w > ev_cap_on_w:
        decision = "cap_on"
        desired_value = str(cap_on_int)
    elif ev_w < ev_cap_off_w:
        decision = "cap_off"
        desired_value = str(normal_int)
    else:
        decision = "hold"
        desired_value = current_raw

    writes: list[tuple[str, str, str]] = []
    if not _numeric_equal(current_raw, desired_value):
        writes.append((hold_param, desired_value, "ev_discharge_cap"))

    top_up_ctx: dict[str, Any] | None = None
    top_up_error = False
    if top_up_config is not None:
        ac_charge_raw = current_values[top_up_hold_param]
        try:
            ac_charge_enabled = _parse_bool_setting(ac_charge_raw)
            local_now = datetime.now(top_up_config["timezone"])
            top_up_phase, desired_ac_charge = _top_up_decision(
                local_now,
                top_up_config["start"],
                top_up_config["end"],
                top_up_config["arm_minutes"],
                top_up_config["target_soc"],
                soc,
                ac_charge_enabled,
            )
        except ValueError as e:
            _emit({**ctx, "decision": "error", "action": "none",
                   "verify": "skipped", "current_value": current_raw,
                   "desired_value": desired_value,
                   "error": f"top-up decision: {e}"})
            return 2

        desired_ac_charge_raw = "true" if desired_ac_charge else "false"
        top_up_error = top_up_phase == "telemetry_unavailable"
        current_target_raw = current_values[top_up_soc_hold_param]
        target_raw = str(top_up_config["target_soc"])
        # The feature owns this limit while enabled, ensuring the inverter's
        # native stop threshold agrees with the controller's threshold.
        if not _numeric_equal(current_target_raw, target_raw):
            writes.insert(
                0, (top_up_soc_hold_param, target_raw, "top_up_target"),
            )
        if ac_charge_enabled != desired_ac_charge:
            writes.append(
                (top_up_hold_param, desired_ac_charge_raw, "top_up_mode"),
            )
        top_up_ctx = {
            "enabled": True,
            "phase": top_up_phase,
            "local_time": local_now.isoformat(),
            "timezone": top_up_config["timezone_name"],
            "window": (
                f"{top_up_config['start'].strftime('%H:%M')}-"
                f"{top_up_config['end'].strftime('%H:%M')}"
            ),
            "target_soc": top_up_config["target_soc"],
            "current_target_soc": current_target_raw,
            "ac_charge_current": ac_charge_raw,
            "ac_charge_desired": desired_ac_charge_raw,
            "error": (
                "battery SOC unavailable; forcing AC Charge off"
                if top_up_error else None
            ),
        }

    log_ctx = {
        **ctx,
        "decision": decision,
        "current_value": current_raw,
        "desired_value": desired_value,
        "dry_run": dry_run,
        "top_up": top_up_ctx,
    }
    if not writes:
        reason = ("in hysteresis hold zone" if decision == "hold"
                  else "all settings already at desired values")
        _emit({**log_ctx, "action": "none", "verify": "skipped",
               "reason": reason})
        return 2 if top_up_error else 0

    planned_writes = [
        {"hold_param": param, "value": value, "purpose": purpose}
        for param, value, purpose in writes
    ]
    if dry_run:
        _emit({**log_ctx, "action": "would_write", "verify": "skipped",
               "writes": planned_writes})
        return 2 if top_up_error else 0

    write_results: list[dict[str, Any]] = []
    for param, value, purpose in writes:
        ok, write_resp, write_attempts = await _write_hold_param_with_retry(
            api, param, value,
        )
        result = {
            "hold_param": param,
            "value": value,
            "purpose": purpose,
            "success": ok,
            "attempts": write_attempts,
            "msg": write_resp.get("msg"),
            "msg_code": write_resp.get("msgCode"),
        }
        write_results.append(result)
        if not ok:
            _emit({**log_ctx, "action": "write", "verify": "skipped",
                   "writes": write_results,
                   "error": f"write failed for {param} after "
                            f"{write_attempts} attempts: "
                            f"{write_resp.get('msg') or write_resp.get('error') or 'unknown'}"})
            return 1

    verify_values, _, verify_attempts = await _read_hold_params_with_retry(
        api, tuple(param for param, _, _ in writes), setting_banks,
    )
    verify_results = []
    verify_ok = True
    for param, desired, purpose in writes:
        actual = verify_values.get(param)
        if param == top_up_hold_param and actual is not None:
            try:
                matches = _parse_bool_setting(actual) == _parse_bool_setting(desired)
            except ValueError:
                matches = False
        else:
            matches = actual is not None and _numeric_equal(actual, desired)
        verify_ok = verify_ok and matches
        verify_results.append({
            "hold_param": param,
            "purpose": purpose,
            "desired": desired,
            "actual": actual,
            "pass": matches,
        })

    verify = "pass" if verify_ok else "fail"
    _emit({**log_ctx, "action": "write", "verify": verify,
           "writes": write_results, "verify_results": verify_results,
           "verify_attempts": verify_attempts})
    if not verify_ok:
        return 1
    return 2 if top_up_error else 0


def _numeric_equal(a: str, b: str) -> bool:
    try:
        return float(a) == float(b)
    except (TypeError, ValueError):
        return str(a).strip() == str(b).strip()


def main() -> int:
    _setup_logging()
    args = _parse_args()
    return asyncio.run(_run(args))


if __name__ == "__main__":
    sys.exit(main())
