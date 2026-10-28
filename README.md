# Business Intelligence Agent

Sends a daily BI summary email to an internal newsletter.

```
EventBridge Scheduler (08:00 America/New_York)
        │
        ▼
Lambda: bi-agent-daily-report ──SQL (RDS Data API)──► Aurora Serverless v2 PostgreSQL
        │                                              ├─ sales_db.product_sales      (10 rows)
        │                                              └─ prospects_db.inbound_prospects (10 rows)
        ├──InvokeAgent──► Bedrock Agent "business-intelligence-agent" (alias: live, Claude Sonnet 4.5)
        │                  → 1-sentence change summary + 1-sentence stakeholder note
        └──SendEmail────► SES → newsletter recipients (verified addresses)
```

See [PLAN.md](PLAN.md) for the design decisions and build steps.

## Layout
- `infra/`: CDK (TypeScript) stack `BusinessIntelligenceAgent`
- `lambda/report/handler.py`: queries both DBs, calls the agent, compiles and sends the email
- `lambda/seed/handler.py`: creates both databases and loads fake data (custom resource + manual reseed)

## Deploy
```bash
cd infra
npm install
npx cdk deploy
```
Before deploying, copy `infra/config.example.json` to `infra/config.json` (gitignored) and set `emailAddress`, `additionalRecipients` and `senderAddress`. The region (`us-east-1`) and the model are set in `infra/cdk.json`, and the account is whichever one your AWS credentials point at. Full walkthrough: [SETUP.md](SETUP.md). Each new recipient gets a verification email and must click it before sends succeed (SES sandbox).

After the first deploy, click the SES verification link sent to the email address. SES is in sandbox mode, so only verified addresses can send or receive.

## Manual trigger
- **Console:** open the `ManualTriggerConsoleUrl` stack output (Lambda → `bi-agent-daily-report` → Test) and run any event, e.g. `{"source": "manual"}`.
- **CLI:** `cd infra && npm run trigger`. The response includes the email body.
- **Dry run (no email):** `npm run trigger:dry`, or pass `{"dryRun": true}` as the console test event.

## Reseed fake data
Seed timestamps are relative to seed time (5 rows in the last 24h and 5 rows 2–7 days old per table), so the "last 24h" window empties after a day. Refresh it with:
```bash
cd infra && npm run reseed
```

## Notes
- Aurora scales to 0 ACU when idle. The first query after a pause waits about 15s while it resumes, and the Lambdas retry automatically.
- Nothing is publicly reachable: the DB is in isolated subnets, and access goes only through the IAM-authenticated Data API.

## Teardown
```bash
cd infra && npx cdk destroy
```

## Gotcha: shell region
If your shell sets `AWS_REGION`, it overrides `~/.aws/config` for CLI commands. The stack always deploys to the region in `cdk.json` (**us-east-1**), and the npm scripts pin `--region us-east-1`, so add that flag to any other CLI commands.
