from datetime import datetime, time
from types import SimpleNamespace
from unittest import IsolatedAsyncioTestCase, TestCase
from unittest.mock import AsyncMock, call, patch
from zoneinfo import ZoneInfo

import guardrail
from guardrail import _parse_bool_setting, _parse_hhmm, _top_up_decision


class TopUpDecisionTests(TestCase):
    timezone = ZoneInfo("America/Los_Angeles")
    start = time(14, 0)
    end = time(15, 0)

    def decide(self, hour, minute, soc, enabled):
        now = datetime(2026, 8, 10, hour, minute, tzinfo=self.timezone)
        return _top_up_decision(
            now, self.start, self.end, 30, 65, soc, enabled,
        )

    def test_arms_before_window_when_soc_is_low(self):
        self.assertEqual(self.decide(13, 45, 40, False), ("arm", True))

    def test_does_not_arm_before_window_when_target_is_met(self):
        self.assertEqual(
            self.decide(13, 45, 65, False), ("not_needed", False),
        )

    def test_keeps_charging_below_target(self):
        self.assertEqual(self.decide(14, 20, 64, True), ("charging", True))

    def test_disables_at_target(self):
        self.assertEqual(
            self.decide(14, 20, 65, True), ("target_reached", False),
        )

    def test_does_not_restart_after_reaching_target(self):
        self.assertEqual(self.decide(14, 40, 63, False), ("complete", False))

    def test_disables_after_window(self):
        self.assertEqual(self.decide(15, 0, 40, True), ("standby", False))

    def test_disables_when_soc_telemetry_is_unavailable(self):
        self.assertEqual(
            self.decide(14, 20, None, True),
            ("telemetry_unavailable", False),
        )

    def test_rejects_overnight_window(self):
        with self.assertRaisesRegex(ValueError, "same day"):
            _top_up_decision(
                datetime(2026, 8, 10, 23, 0, tzinfo=self.timezone),
                time(23, 0),
                time(1, 0),
                30,
                65,
                40,
                True,
            )


class TopUpParsingTests(TestCase):
    def test_parses_time(self):
        self.assertEqual(_parse_hhmm("14:05", "START"), time(14, 5))

    def test_parses_cloud_boolean_values(self):
        for value in ("true", "1", "ON", "enabled"):
            self.assertTrue(_parse_bool_setting(value))
        for value in ("false", "0", "OFF", "disabled"):
            self.assertFalse(_parse_bool_setting(value))


class FrozenDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 10, 14, 20)
        return value.replace(tzinfo=tz) if tz else value


class FrozenArmDateTime(datetime):
    @classmethod
    def now(cls, tz=None):
        value = cls(2026, 8, 10, 13, 45)
        return value.replace(tzinfo=tz) if tz else value


class TopUpIntegrationTests(IsolatedAsyncioTestCase):
    env = {
        "EG4_TOP_UP_ENABLED": "1",
        "EG4_TOP_UP_TIMEZONE": "America/Los_Angeles",
        "EG4_TOP_UP_START": "14:00",
        "EG4_TOP_UP_END": "15:00",
        "EG4_TOP_UP_TARGET_SOC": "65",
        "EG4_DRY_RUN": "0",
    }

    def runtime(self, soc):
        return SimpleNamespace(success=True, soc=soc, ppv=1000)

    async def test_completed_top_up_does_not_restart_after_soc_dips(self):
        api = SimpleNamespace(
            get_inverter_runtime_async=AsyncMock(return_value=self.runtime(63)),
        )
        current = {
            "HOLD_DISCHG_CUT_OFF_SOC_EOD": "2",
            "FUNC_AC_CHARGE": "false",
            "HOLD_AC_CHARGE_SOC_LIMIT": "65",
        }
        with (
            patch.dict(guardrail.os.environ, self.env, clear=True),
            patch.object(guardrail, "datetime", FrozenDateTime),
            patch.object(guardrail, "_resolve_gridboss_serial", return_value="GB"),
            patch.object(
                guardrail, "_read_ev_power", AsyncMock(return_value=(0.0, None)),
            ),
            patch.object(
                guardrail,
                "_read_hold_params_with_retry",
                AsyncMock(return_value=(current, {}, 1)),
            ),
            patch.object(
                guardrail, "_write_hold_param_with_retry", new_callable=AsyncMock,
            ) as write,
            patch.object(guardrail, "_emit"),
        ):
            result = await guardrail._decide_and_write(
                api, SimpleNamespace(dry_run=False, apply=False),
            )

        self.assertEqual(result, 0)
        write.assert_not_awaited()

    async def test_reaching_target_disables_ac_charge(self):
        api = SimpleNamespace(
            get_inverter_runtime_async=AsyncMock(return_value=self.runtime(65)),
        )
        current = {
            "HOLD_DISCHG_CUT_OFF_SOC_EOD": "2",
            "FUNC_AC_CHARGE": "true",
            "HOLD_AC_CHARGE_SOC_LIMIT": "65",
        }
        read = AsyncMock(side_effect=[
            (current, {}, 1),
            ({"FUNC_AC_CHARGE": "false"}, {}, 1),
        ])
        write = AsyncMock(return_value=(True, {"success": True}, 1))
        with (
            patch.dict(guardrail.os.environ, self.env, clear=True),
            patch.object(guardrail, "datetime", FrozenDateTime),
            patch.object(guardrail, "_resolve_gridboss_serial", return_value="GB"),
            patch.object(
                guardrail, "_read_ev_power", AsyncMock(return_value=(0.0, None)),
            ),
            patch.object(guardrail, "_read_hold_params_with_retry", read),
            patch.object(guardrail, "_write_hold_param_with_retry", write),
            patch.object(guardrail, "_emit"),
        ):
            result = await guardrail._decide_and_write(
                api, SimpleNamespace(dry_run=False, apply=False),
            )

        self.assertEqual(result, 0)
        write.assert_awaited_once_with(api, "FUNC_AC_CHARGE", "false")

    async def test_arm_sets_target_before_enabling_ac_charge(self):
        api = SimpleNamespace(
            get_inverter_runtime_async=AsyncMock(return_value=self.runtime(40)),
        )
        current = {
            "HOLD_DISCHG_CUT_OFF_SOC_EOD": "2",
            "FUNC_AC_CHARGE": "false",
            "HOLD_AC_CHARGE_SOC_LIMIT": "60",
        }
        read = AsyncMock(side_effect=[
            (current, {}, 1),
            ({
                "HOLD_AC_CHARGE_SOC_LIMIT": "65",
                "FUNC_AC_CHARGE": "true",
            }, {}, 1),
        ])
        write = AsyncMock(return_value=(True, {"success": True}, 1))
        with (
            patch.dict(guardrail.os.environ, self.env, clear=True),
            patch.object(guardrail, "datetime", FrozenArmDateTime),
            patch.object(guardrail, "_resolve_gridboss_serial", return_value="GB"),
            patch.object(
                guardrail, "_read_ev_power", AsyncMock(return_value=(0.0, None)),
            ),
            patch.object(guardrail, "_read_hold_params_with_retry", read),
            patch.object(guardrail, "_write_hold_param_with_retry", write),
            patch.object(guardrail, "_emit"),
        ):
            result = await guardrail._decide_and_write(
                api, SimpleNamespace(dry_run=False, apply=False),
            )

        self.assertEqual(result, 0)
        self.assertEqual(write.await_args_list, [
            call(api, "HOLD_AC_CHARGE_SOC_LIMIT", "65"),
            call(api, "FUNC_AC_CHARGE", "true"),
        ])

    async def test_missing_soc_disables_top_up_without_suppressing_ev_cap(self):
        api = SimpleNamespace(
            get_inverter_runtime_async=AsyncMock(return_value=self.runtime(None)),
        )
        current = {
            "HOLD_DISCHG_CUT_OFF_SOC_EOD": "2",
            "FUNC_AC_CHARGE": "true",
            "HOLD_AC_CHARGE_SOC_LIMIT": "65",
        }
        read = AsyncMock(side_effect=[
            (current, {}, 1),
            ({
                "HOLD_DISCHG_CUT_OFF_SOC_EOD": "100",
                "FUNC_AC_CHARGE": "false",
            }, {}, 1),
        ])
        write = AsyncMock(return_value=(True, {"success": True}, 1))
        with (
            patch.dict(guardrail.os.environ, self.env, clear=True),
            patch.object(guardrail, "datetime", FrozenDateTime),
            patch.object(guardrail, "_resolve_gridboss_serial", return_value="GB"),
            patch.object(
                guardrail, "_read_ev_power",
                AsyncMock(return_value=(2000.0, None)),
            ),
            patch.object(guardrail, "_read_hold_params_with_retry", read),
            patch.object(guardrail, "_write_hold_param_with_retry", write),
            patch.object(guardrail, "_emit"),
        ):
            result = await guardrail._decide_and_write(
                api, SimpleNamespace(dry_run=False, apply=False),
            )

        self.assertEqual(result, 2)
        self.assertEqual(write.await_args_list, [
            call(api, "HOLD_DISCHG_CUT_OFF_SOC_EOD", "100"),
            call(api, "FUNC_AC_CHARGE", "false"),
        ])


class FunctionWriteTests(IsolatedAsyncioTestCase):
    async def test_function_param_uses_function_control_endpoint(self):
        api = SimpleNamespace(
            _base_url="https://example.test",
            _serialNum="inverter",
            _request=AsyncMock(return_value={"success": True}),
        )

        success, _, attempts = await guardrail._write_hold_param_with_retry(
            api, "FUNC_AC_CHARGE", "true",
        )

        self.assertTrue(success)
        self.assertEqual(attempts, 1)
        api._request.assert_awaited_once_with(
            "POST",
            "https://example.test/WManage/web/maintain/remoteSet/functionControl",
            "inverterSn=inverter&functionParam=FUNC_AC_CHARGE&enable=true"
            "&clientType=WEB&remoteSetType=NORMAL",
        )
