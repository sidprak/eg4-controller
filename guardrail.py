#!/usr/bin/env python3
"""EG4 battery discharge guardrail.

Stateless script intended for cron. Each run:
  authenticate → read telemetry → decide desired discharge cap →
  read current setting → write only if different → verify by re-read →
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
from datetime import datetime, timezone
from typing import Any

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

# Default hold-register key. UI label: "Start Discharge P_import(W)".
# Meaning: battery only starts discharging TO ON-GRID LOADS once the power
# imported from the grid exceeds this many watts. The factory default of
# ~100 W means "any load triggers battery discharge". Setting this to a
# value well above the home's peak possible grid-import (e.g. 30000 W)
# effectively forbids on-grid battery discharge: loads that exceed PV
# pull from grid instead of from the battery. Off-grid/EPS discharge is
# unaffected (that uses a different code path).
#
# An earlier version of this script targeted HOLD_DISCHG_CUT_OFF_SOC_EOD
# ("On-Grid Cut-Off SOC %") set to 100 — that turned out to put the
# inverter into "end-of-discharge reached -> grid bypass" mode, which
# DEFEATED the goal by also disabling PV->loads pass-through. Use
# P_TO_USER_START_DISCHG instead: same end result for battery output,
# but PV continues to serve loads normally.
DEFAULT_HOLD_PARAM_DISCHARGE = "HOLD_P_TO_USER_START_DISCHG"

# Default threshold (watts) written when the cap is ON. 30000 is well
# above any realistic home grid-import, so battery never starts
# discharging to on-grid loads while this is set. The register is a
# signed 16-bit int on FlexBOSS21 (max 32767); leave headroom.
DEFAULT_CAP_ON_THRESHOLD_W = "30000"

# Default threshold (watts) restored when the cap is OFF. 100 matches the
# typical FlexBOSS21 factory setting; set whatever shows in the EG4 web
# UI as "Start Discharge P_import(W)" today.
DEFAULT_NORMAL_THRESHOLD_W = "100"

# Inverter register accepts a signed 16-bit value. Anything outside this
# range will be rejected (or wrap) and is almost certainly a config typo.
THRESHOLD_W_MIN = 0
THRESHOLD_W_MAX = 32767


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
    pv_w = record.get("pv_w")
    current = record.get("current_value")
    desired = record.get("desired_value")
    summary = (
        f"decision={decision} pv_w={pv_w} current={current} "
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


async def _read_hold_param_with_retry(
    api: EG4InverterAPI,
    hold_param: str,
    banks: tuple[int, ...],
) -> tuple[str | None, dict[int, dict[str, Any]], int]:
    """Read `hold_param` via the bank sweep, retrying when missing or transient.

    Returns (value_or_None, last_bank_status, attempts). The EG4 cloud
    sometimes returns success=true for a bank but omits individual keys, so
    we retry on "value not found" as well as on DEVICE_OFFLINE storms.
    """
    last_status: dict[int, dict[str, Any]] = {}
    for attempt in range(1, READ_MAX_ATTEMPTS + 1):
        settings, last_status = await _read_settings_tolerant(api, banks)
        val = _extract_setting_value(settings, hold_param)
        if val is not None:
            return val, last_status, attempt
        if attempt < READ_MAX_ATTEMPTS:
            reason = ("transient cloud error"
                      if _bank_status_has_transient(last_status)
                      else "key missing")
            logging.info(
                "settings read attempt %d/%d for %s: %s; sleeping %ds and retrying",
                attempt, READ_MAX_ATTEMPTS, hold_param, reason, READ_RETRY_BACKOFF_S,
            )
            await asyncio.sleep(READ_RETRY_BACKOFF_S)
    return None, last_status, READ_MAX_ATTEMPTS


async def _write_hold_param_with_retry(
    api: EG4InverterAPI,
    hold_param: str,
    value_text: str,
) -> tuple[bool, dict[str, Any], int]:
    """Write a hold register, retrying transient cloud errors.

    Returns (success, last_response, attempts). We bypass the library's
    write_setting_async because it discards the response body; we need the
    msgCode to decide whether retrying is worthwhile.
    """
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
    print(json.dumps(
        {
            "serial": api._serialNum,
            "bank_status": bank_status,
            "settings": settings,
            "discharge_candidates": discharge_candidates,
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
    # Hysteresis thresholds. Two separate W cutoffs so spiky/cloudy days don't
    # flip the cap every 15 min. Defaults derived from EG4_PV_THRESHOLD_W (legacy
    # single-threshold var) if the new ones aren't explicitly set, so existing
    # deploys keep their behavior unless reconfigured.
    legacy = _env_int("EG4_PV_THRESHOLD_W", 1700)
    pv_cap_on_w = _env_int("EG4_PV_CAP_ON_W", legacy)
    pv_cap_off_w = _env_int("EG4_PV_CAP_OFF_W", max(0, legacy - 500))
    # Hold-register threshold values (watts).
    normal_threshold = os.getenv(
        "EG4_NORMAL_DISCHARGE_THRESHOLD_W", DEFAULT_NORMAL_THRESHOLD_W,
    )
    cap_on_threshold = os.getenv(
        "EG4_CAP_ON_THRESHOLD_W", DEFAULT_CAP_ON_THRESHOLD_W,
    )
    hold_param = os.getenv("EG4_HOLD_PARAM_DISCHARGE", DEFAULT_HOLD_PARAM_DISCHARGE)
    pv_field = os.getenv("EG4_PV_FIELD", "ppv")

    if pv_cap_on_w < pv_cap_off_w:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_PV_CAP_ON_W ({pv_cap_on_w}) must be >= "
                        f"EG4_PV_CAP_OFF_W ({pv_cap_off_w}); otherwise "
                        "hysteresis logic is inverted"})
        return 2

    try:
        normal_int = int(float(normal_threshold))
    except ValueError:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_NORMAL_DISCHARGE_THRESHOLD_W not numeric: "
                        f"{normal_threshold!r}"})
        return 2
    try:
        cap_on_int = int(float(cap_on_threshold))
    except ValueError:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_THRESHOLD_W not numeric: "
                        f"{cap_on_threshold!r}"})
        return 2
    if not THRESHOLD_W_MIN <= normal_int <= THRESHOLD_W_MAX:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_NORMAL_DISCHARGE_THRESHOLD_W must be "
                        f"{THRESHOLD_W_MIN}..{THRESHOLD_W_MAX}, got {normal_int}"})
        return 2
    if not THRESHOLD_W_MIN <= cap_on_int <= THRESHOLD_W_MAX:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_THRESHOLD_W must be "
                        f"{THRESHOLD_W_MIN}..{THRESHOLD_W_MAX}, got {cap_on_int}"})
        return 2
    if cap_on_int <= normal_int:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"EG4_CAP_ON_THRESHOLD_W ({cap_on_int}) must be > "
                        f"EG4_NORMAL_DISCHARGE_THRESHOLD_W ({normal_int}); "
                        "otherwise cap-on would not raise the threshold"})
        return 2

    dry_run = _env_bool("EG4_DRY_RUN", True)
    if args.dry_run:
        dry_run = True
    if args.apply:
        dry_run = False

    rt = await api.get_inverter_runtime_async()
    if hasattr(rt, "success") and rt.success is False:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "error": f"runtime read failed: {getattr(rt, 'error_message', '?')}"})
        return 2

    pv_w = _extract_pv_w(rt, pv_field)
    if pv_w is None:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "pv_field": pv_field,
               "error": f"PV field '{pv_field}' missing/unparseable in runtime data"})
        return 2

    # Read current setting first; needed both for hold-zone behavior and the
    # idempotency check below. Retry on transient DEVICE_OFFLINE storms or
    # responses that omit the hold-param key.
    setting_banks = _parse_setting_banks(os.getenv("EG4_SETTING_BANKS"))
    current_raw, bank_status, read_attempts = await _read_hold_param_with_retry(
        api, hold_param, setting_banks,
    )
    if current_raw is None:
        _emit({"decision": "error", "action": "none", "verify": "skipped",
               "pv_w": pv_w, "hold_param": hold_param,
               "bank_status": bank_status, "read_attempts": read_attempts,
               "error": f"hold_param '{hold_param}' not found after "
                        f"{read_attempts} settings-read attempts; "
                        "run --discover to confirm key / firmware health"})
        return 2

    # Three-zone hysteresis:
    #   PV >  cap_on_w   → cap_on  (write cap_on threshold)
    #   PV <  cap_off_w  → cap_off (write normal threshold)
    #   between          → hold    (leave current value alone)
    # The "hold" zone is what prevents flapping when PV spikes around a single
    # threshold. We define `desired_value = current_raw` so the idempotency
    # check below will naturally short-circuit with action=none.
    if pv_w > pv_cap_on_w:
        decision = "cap_on"
        desired_value = str(cap_on_int)
    elif pv_w < pv_cap_off_w:
        decision = "cap_off"
        desired_value = str(normal_int)
    else:
        decision = "hold"
        desired_value = current_raw

    if _numeric_equal(current_raw, desired_value):
        reason = ("in hysteresis hold zone" if decision == "hold"
                  else "already at desired value")
        _emit({"decision": decision, "action": "none", "verify": "skipped",
               "pv_w": pv_w, "pv_cap_on_w": pv_cap_on_w,
               "pv_cap_off_w": pv_cap_off_w,
               "current_value": current_raw, "desired_value": desired_value,
               "hold_param": hold_param, "dry_run": dry_run,
               "reason": reason})
        return 0

    if dry_run:
        _emit({"decision": decision, "action": "would_write", "verify": "skipped",
               "pv_w": pv_w, "pv_cap_on_w": pv_cap_on_w,
               "pv_cap_off_w": pv_cap_off_w,
               "current_value": current_raw, "desired_value": desired_value,
               "hold_param": hold_param, "dry_run": True})
        return 0

    ok, write_resp, write_attempts = await _write_hold_param_with_retry(
        api, hold_param, desired_value,
    )
    if not ok:
        _emit({"decision": decision, "action": "write", "verify": "skipped",
               "pv_w": pv_w, "pv_cap_on_w": pv_cap_on_w,
               "pv_cap_off_w": pv_cap_off_w,
               "current_value": current_raw, "desired_value": desired_value,
               "hold_param": hold_param, "dry_run": False,
               "write_attempts": write_attempts,
               "write_msg": write_resp.get("msg"),
               "write_msg_code": write_resp.get("msgCode"),
               "error": f"write failed after {write_attempts} attempts: "
                        f"{write_resp.get('msg') or write_resp.get('error') or 'unknown'}"})
        return 1

    verify_raw, _, verify_attempts = await _read_hold_param_with_retry(
        api, hold_param, setting_banks,
    )
    if verify_raw is None:
        verify = "unknown"
    elif _numeric_equal(verify_raw, desired_value):
        verify = "pass"
    else:
        verify = "fail"
    _emit({"decision": decision, "action": "write", "verify": verify,
           "pv_w": pv_w, "pv_cap_on_w": pv_cap_on_w,
           "pv_cap_off_w": pv_cap_off_w,
           "current_value": current_raw, "desired_value": desired_value,
           "post_write_value": verify_raw, "hold_param": hold_param,
           "dry_run": False, "write_attempts": write_attempts,
           "verify_attempts": verify_attempts})
    return 0 if verify == "pass" else 1


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
