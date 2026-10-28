# Business Intelligence Agent — Development Plan

Daily newsletter service: EventBridge Scheduler → Report Lambda → SQL over two Aurora (RDS)
databases → Bedrock Agent summary → SES email.

## Fixed decisions

| Item | Choice | Reason |
|---|---|---|
| Account / region | Account from the active AWS credentials / `us-east-1` (`cdk.json`) | Portable across accounts |
| IaC | AWS CDK v2, TypeScript (`infra/`) | CDK CLI already installed |
| Lambda runtime | Python 3.13, boto3 from runtime (no bundling) | Simple, no build step |
| Database | One Aurora Serverless v2 PostgreSQL 16.15 cluster, **two databases**: `sales_db`, `prospects_db` | Data API = no VPC Lambda, no NAT, no VPC endpoints |
| DB capacity | 0–1 ACU with auto-pause | Near-zero idle cost; Lambda retries `DatabaseResumingException` |
| DB network | New VPC, isolated subnets only, not publicly accessible | Nothing reachable from the internet; access is IAM-authenticated Data API only |
| AI | Bedrock Agent (no action groups) on `us.anthropic.claude-sonnet-4-5-20250929-v1:0`, alias `live` | Matches "Bedrock agent" spec. Sonnet 5.5 was rejected: Agents orchestration sends `thinking.type: enabled`, which 5.5 does not accept |
| Email | SES v2; sender and recipients set in gitignored `infra/config.json` (sandbox, so every identity must be verified) | Keeps personal addresses out of the repo |
| Schedule | EventBridge Scheduler `cron(0 8 * * ? *)`, `America/New_York` | User choice |
| Manual trigger | Lambda console **Test** button + `npm run trigger` (CLI invoke) | User asked for a manual trigger; no public URL |
| Removal policy | `DESTROY` everywhere (fake data, dev stack) | Easy teardown |

## Steps

1. **Scaffold** `infra/` CDK TypeScript app (`bin/app.ts`, `lib/bi-agent-stack.ts`), `lambda/report/`, `lambda/seed/`, `README.md`.
2. **VPC**: 2 AZs, `PRIVATE_ISOLATED` subnets only, 0 NAT gateways.
3. **Aurora cluster**: `aurora-postgresql` 16.15, Serverless v2 writer, `serverlessV2MinCapacity: 0`, max 1, `enableDataApi: true`, generated secret in Secrets Manager, default DB name `postgres`.
4. **Seed Lambda + custom resource** (`lambda/seed/handler.py`), runs via Data API on stack create/update:
   - `CREATE DATABASE sales_db` / `prospects_db` if missing.
   - `sales_db.product_sales(id, order_id, product_name, region, units_sold, unit_price, revenue, sold_at)` with 10 rows.
   - `prospects_db.inbound_prospects(id, company_name, contact_name, industry, company_size, interest_level, estimated_budget, source, notes, submitted_at)` with 10 rows.
   - Timestamps relative to `NOW()`: about 5 rows in the last 24h and 5 rows 2–7 days old, so the 24h comparison has data.
   - Truncate and reinsert on every run so a manual reseed is idempotent.
5. **Bedrock Agent**: IAM role allowing `bedrock:InvokeModel*` on the inference profile plus its 3 regional foundation-model ARNs, and `bedrock:GetInferenceProfile`. Instruction: return strict JSON `{"changes_summary": "<1 sentence>", "stakeholder_note": "<1 sentence>"}`. `autoPrepare: true`. Alias `live`.
6. **Report Lambda** (`lambda/report/handler.py`):
   1. Data API queries (retry on resume): last-24h rows and prior-6-day aggregates from both DBs.
   2. Build a JSON payload and call `bedrock-agent-runtime.invoke_agent` (new session per run), collect the streamed chunks, and parse the JSON.
   3. Compile a plain-text email: headline metrics, 2 summary sentences, and the raw 24h rows.
   4. Send with `sesv2.send_email`. Return the summary in the response for manual runs.
   - IAM: `rds-data:ExecuteStatement` on the cluster, `secretsmanager:GetSecretValue` on the secret, `bedrock:InvokeAgent` on the alias, `ses:SendEmail` on the identity.
   - Timeout 2 min (covers DB resume and the agent call).
7. **SES**: an `EmailIdentity` for each configured address (each one triggers a verification email).
8. **Scheduler**: `CfnSchedule` with a dedicated role that has `lambda:InvokeFunction`, daily 08:00 America/New_York.
9. **Outputs**: function name, console test URL, cluster ARN, agent ID and alias ID.
10. **Build/verify**: `npm run build`, `cdk synth`, `cdk bootstrap` (account not bootstrapped), `cdk deploy`.
11. **Verify**: user clicks the SES verification link; run `npm run trigger`; check the Lambda response and logs, and confirm the email arrives.
12. **Docs**: README covering architecture, deploy, manual trigger, reseed, and teardown.

## Known limitations
- Seed timestamps are relative to seed time. After about 24h the "last 24h" window is empty until you reseed (`npm run reseed`).
- SES sandbox: only verified addresses can receive mail. Production access is needed for real newsletter lists.
- First query after idle waits about 15s for Aurora to resume from 0 ACU.

## Change log
- **2026-10-28: Aurora Serverless v2 replaced with RDS for PostgreSQL 16.15** (`db.t4g.micro`, 20 GB gp3, single-AZ, IAM auth on). RDS for PostgreSQL has no Data API, so the SQL moved to an in-VPC `bi-agent-db-query` Lambda (pg8000, TLS, IAM token as `bi_reader`) that the report Lambda invokes. The seed Lambda moved into the VPC and reads the admin secret through a single-AZ Secrets Manager interface endpoint. Fixed cost rises to about $22/month; there's no more resume delay.
- **2026-10-28: Reverted to Aurora Serverless v2** to bring the fixed cost back to about $0.50/month. RDS for PostgreSQL works, but needs in-VPC Lambdas and a Secrets Manager endpoint, about $22/month fixed.
