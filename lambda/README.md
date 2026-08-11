# Deploy to AWS Lambda

Stateless Lambda container image, scheduled by EventBridge every 30 min.
Cost: **$0/month** within the AWS perpetual free tier (1 M Lambda requests
+ 400 k GB-s/mo + 14 M EventBridge invocations + 5 GB CloudWatch ingest).

## Prereqs

```sh
brew install awscli aws-sam-cli docker            # macOS
aws configure                                     # set up IAM creds
open -a Docker                                    # SAM build needs the daemon running
```

Your AWS user needs permission to create IAM roles, Lambda functions, ECR
repos, EventBridge rules, and CloudWatch log groups. `AdministratorAccess`
is the easiest for a personal account.

## First deploy

From the **repository root** (not the `lambda/` directory):

```sh
sam build --template lambda/template.yaml
sam deploy --guided --template lambda/template.yaml
```

The `--guided` wizard will ask for:

- **Stack name** — e.g. `eg4-guardrail`
- **AWS Region** — e.g. `us-east-1`
- **EG4Username / EG4Password** — your account creds (NoEcho — not printed,
  not stored in CloudFormation events)
- **EG4SerialNumber** — leave blank to use the first inverter on your
  account, or paste your inverter SN
- **DryRun** — leave `1` for the first deploy
- **TopUpEnabled** — set `1` only after configuring the inverter's native
  AC Charge **According to Time** window to match `TopUpStart` / `TopUpEnd`
- **TopUpTimezone** — `America/Los_Angeles`
- **AlarmEmail** — *(optional)* email to receive sustained-failure
  alerts. Leave blank to skip. See [Alerting](#alerting) below.
- (other params) — defaults match the verified FlexBOSS21 setup
- **Save arguments to configuration file** — say **yes**. SAM writes
  `samconfig.toml` so future deploys are one command:

  ```sh
  sam deploy --template lambda/template.yaml
  ```

`samconfig.toml` will contain your EG4 password in plaintext. The repo
`.gitignore` already excludes it.

## Verify the deploy

```sh
# Tail logs (Ctrl-C to stop)
sam logs --stack-name eg4-guardrail --tail

# Or invoke once manually
aws lambda invoke --function-name $(aws cloudformation describe-stacks \
  --stack-name eg4-guardrail \
  --query 'Stacks[0].Outputs[?OutputKey==`FunctionName`].OutputValue' \
  --output text) /tmp/out.json && cat /tmp/out.json
```

You should see the same structured log line we saw locally:

```
decision=cap_off ev_w=0.0 current=2 desired=2 action=none verify=skipped | {...}
```

## Run discover from Lambda

```sh
FN=$(aws cloudformation describe-stacks --stack-name eg4-guardrail \
     --query 'Stacks[0].Outputs[?OutputKey==`FunctionName`].OutputValue' \
     --output text)
aws lambda invoke --function-name "$FN" \
  --payload '{"discover": true}' --cli-binary-format raw-in-base64-out \
  /tmp/discover.json
```

## Flip from dry-run to live

After a day of dry-run looks correct in CloudWatch:

```sh
sam deploy --template lambda/template.yaml \
  --parameter-overrides DryRun=0
```

(Or edit `samconfig.toml` and run `sam deploy`.)

## Tweak schedule

The default is `cron(*/30 * * * ? *)` — every 30 min UTC. To run every 15
min instead:

```sh
sam deploy --template lambda/template.yaml \
  --parameter-overrides 'ScheduleExpression="cron(*/15 * * * ? *)"'
```

AWS cron is 6-field with `?` placeholder in either day-of-month or
day-of-week (not both). See
<https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-scheduled-rule-pattern.html>.

## Alerting

Set the `AlarmEmail` parameter on deploy to receive an email when the
guardrail has been failing for a sustained period. If `AlarmEmail` is
blank, no SNS topic or alarm is created.

```sh
sam deploy --template lambda/template.yaml \
  --parameter-overrides AlarmEmail=you@example.com
```

After the deploy AWS will send a one-time **subscription confirmation
email** from `no-reply@sns.amazonaws.com`. Click the link in that email
or no alerts will reach you (the SNS subscription stays in
`PendingConfirmation`).

**What "sustained" means:** the alarm only fires when the Lambda has had
**≥1 error in each of the last 3 consecutive 1-hour windows** (~3 hours
of continuous trouble, ~6 failed invocations on the default 30-min
schedule). Single transient EG4 cloud blips are already absorbed by the
retry logic in `guardrail.py`, so a single bad invocation is almost
always noise and won't trigger an alert.

You'll also receive an "all clear" email when the alarm transitions back
to OK.

**Cost:** $0/month within the AWS perpetual free tier (10 CloudWatch
alarms + 1,000 SNS email notifications + 1 M SNS publishes per month
free, and an alarm that only fires on real outages will use a vanishing
fraction of any of those).

To change the email address later:

```sh
sam deploy --template lambda/template.yaml \
  --parameter-overrides AlarmEmail=newaddress@example.com
```

(SNS will email the new address for confirmation; the old one will be
unsubscribed.)

To disable alerting entirely:

```sh
sam deploy --template lambda/template.yaml \
  --parameter-overrides AlarmEmail=""
```

(Deletes the SNS topic, subscription, and alarm.)

## Teardown

```sh
sam delete --stack-name eg4-guardrail
```

Deletes everything: function, ECR image, EventBridge rule, log group, IAM
role. Reversible — just `sam deploy` again.

## Architecture notes

- **Container image (not zip):** `aiohttp` has C extensions that need Linux
  wheels. A container image sidesteps platform-mismatch issues entirely.
- **arm64 (Graviton):** template defaults to arm64, which is the native
  arch on Apple Silicon (no QEMU emulation when building) and ~20 % cheaper
  Lambda compute. Switch to `x86_64` in `template.yaml` if you're deploying
  from an Intel host without buildx.
- **Secrets:** EG4 username/password are stored as Lambda env vars
  (encrypted at rest with the AWS-managed KMS key, which is free). No
  Secrets Manager dependency.
- **Logs:** retained for 14 days then auto-deleted. Adjust
  `RetentionInDays` in `template.yaml` if you want longer.
- **Cold starts:** container image cold starts are ~1–2 s for this image.
  EG4 cloud latency dominates each invocation (~30–60 s) — cold start is
  noise.
