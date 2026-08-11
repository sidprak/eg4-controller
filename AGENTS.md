# AGENTS.md

## Project overview

This repository contains a Python controller for an EG4 FlexBOSS21 and
GridBoss. It provides:

- An EV discharge guardrail that prevents the battery from backfilling an
  excess-solar EV charger.
- An optional pre-peak AC-charge top-up that charges the battery to a target
  SOC on cloudy days.
- AWS Lambda deployment through SAM and EventBridge.

## Repository layout

- `guardrail.py` - controller logic and CLI entrypoint.
- `test_guardrail.py` - unit and integration-style tests using `unittest`.
- `lambda/lambda_handler.py` - Lambda adapter.
- `lambda/template.yaml` - SAM/CloudFormation deployment template.
- `.env.example` - documented local configuration.
- `.github/workflows/ci.yml` - pull request and `main` branch CI.

## Development

Use Python 3.11. Install dependencies with:

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

Run the same checks as CI:

```sh
.venv/bin/python -m unittest discover -v
.venv/bin/python -m compileall -q guardrail.py lambda test_guardrail.py
```

Validate infrastructure changes with:

```sh
sam validate --lint --template lambda/template.yaml
```

## Implementation guidelines

- Keep the controller stateless across invocations.
- Read related EG4 parameters in one bank sweep to minimize dongle load.
- Make writes idempotent and verify every successful write by reading it back.
- Treat missing or invalid telemetry as fail-safe; never write a guessed state.
- Preserve the existing EV guardrail when changing the optional top-up logic.
- Do not modify the off-grid/EPS discharge floor.
- Use the function-control endpoint for `FUNC_*` switches and the parameter
  write endpoint for `HOLD_*` values.
- Add tests for state transitions and write ordering when behavior changes.

## Configuration and deployment safety

- Never commit `.env`, credentials, inverter serial numbers, or discovery
  dumps.
- Never commit user-, account-, environment-, or deployment-specific details.
  Use generic placeholders in documentation and examples.
- Prefer AWS services and configurations that remain within the AWS Free Tier.
  Notify the user before implementing anything that may incur charges.
- New behavior must be opt-in and documented in `.env.example`.
- Keep `EG4_DRY_RUN=1` during initial local testing.
