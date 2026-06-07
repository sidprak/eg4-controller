# Deploy to AWS Lambda

Stateless Lambda container image, scheduled by EventBridge every 15 min.
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
decision=cap_off pv_w=1465 current_soc=2 desired_soc=2 action=none verify=skipped | {...}
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

The default is `cron(*/15 * * * ? *)` — every 15 min UTC. To run every 30
min instead (halves invocations + cost):

```sh
sam deploy --template lambda/template.yaml \
  --parameter-overrides 'ScheduleExpression="cron(*/30 * * * ? *)"'
```

AWS cron is 6-field with `?` placeholder in either day-of-month or
day-of-week (not both). See
<https://docs.aws.amazon.com/eventbridge/latest/userguide/eb-scheduled-rule-pattern.html>.

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
