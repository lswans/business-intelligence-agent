# Business Intelligence Agent: Setup Guide

Deploy the daily BI newsletter service into your own AWS account.

---

## 1. Prerequisites

- [AWS CLI v2](https://docs.aws.amazon.com/cli/latest/userguide/getting-started-install.html), [Node.js 20+](https://nodejs.org/), and [Git](https://git-scm.com/)
- An **admin IAM Identity Center user or IAM user** (avoid using the root account)

## 2. Sign In

```bash
aws login                     # or: aws configure
aws sts get-caller-identity   # confirm the account ID
```

## 3. Enable Claude in Bedrock (one time)

1. In the AWS console, switch to region **us-east-1**.
2. Go to **Amazon Bedrock → Model catalog → Claude Sonnet 4.5**.
3. Open the model in the playground. If AWS prompts for **Anthropic use-case details**, submit the form; approval usually takes a few minutes.

## 4. Get the Code

```bash
git clone https://github.com/<owner>/business-intelligence-agent.git
cd business-intelligence-agent/infra
npm install
```

## 5. Set Your Email Addresses

Copy `infra/config.example.json` to `infra/config.json` (gitignored, so your addresses never get committed) and edit it:

```json
"emailAddress": "you@example.com",
"additionalRecipients": ["teammate@example.com"],
"senderAddress": "you@example.com"
```

> **Note:** `senderAddress` must also appear in `emailAddress` or `additionalRecipients`. New accounts start in the **SES sandbox**, which means every address must be verified before it can send or receive.

## 6. Deploy (about 10–15 minutes)

```bash
npx cdk bootstrap
npx cdk deploy
```

This creates:

- **Aurora Serverless v2** database loaded with fake sales and prospect data
- Two **Lambda** functions
- A **Bedrock Agent**
- **SES** email identities
- A daily **08:00 US Eastern** EventBridge schedule

## 7. Verify the Email Addresses

Each address gets an *"Amazon Web Services – Email Address Verification Request"* email. **Click the link in every one.** Sends fail until all addresses are verified.

## 8. Test

```bash
npm run trigger:dry   # runs queries + AI summary, no email
npm run trigger       # sends the newsletter
```

You can also go to **Lambda → `bi-agent-daily-report` → Test** and run any event. Add `{"dryRun": true}` to skip the email.

## 9. Day-to-Day Use

| Task | How |
|---|---|
| Daily send | Automatic, every day at 08:00 US Eastern |
| Refresh fake data | After about 24 hours the last-24h window is empty. Run `npm run reseed` |
| Change recipients or model | Edit `config.json` (recipients) or `cdk.json` (model), then run `npx cdk deploy` |

---

## Troubleshooting

| Symptom | Fix |
|---|---|
| **Email missing or in spam** | Check whether the sender's domain enforces DMARC (for example, corporate domains with `p=quarantine`). If it does, Gmail and other providers will filter mail sent through SES. Use a sender whose domain doesn't enforce DMARC, or verify a domain you own in SES with DKIM. |
| **"Access denied" from Bedrock** | Step 3 isn't finished yet. |
| **Stack ends up in the wrong region** | The region comes from `cdk.json` (`us-east-1`). The npm scripts also pass `--region us-east-1`, so any manual CLI command needs the same flag. |
| **Windows** | Run the npm scripts from PowerShell or Git Bash. |

## Cost

About **$0.50/month fixed**: Aurora pauses at 0 capacity when idle, so you pay only for storage and the $0.40/month secret. On top of that, about **$0.023 per newsletter run**, mostly Bedrock tokens plus a few minutes of Aurora uptime. The free tier covers most Lambda, Scheduler, and Secrets Manager API usage.

## Teardown

```bash
npx cdk destroy
```

This deletes everything the stack created, **including the database**.
