"""Daily BI newsletter: query sales + prospects DBs, summarize with a Bedrock agent, email via SES.

Triggered daily by EventBridge Scheduler; can also be run manually (Lambda console Test or
`npm run trigger`) with any JSON payload. Pass {"dryRun": true} to skip sending the email.
"""
import json
import logging
import os
import time
import uuid
from datetime import datetime, timezone

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rds_data = boto3.client("rds-data")
agent_runtime = boto3.client("bedrock-agent-runtime")
sesv2 = boto3.client("sesv2")

CLUSTER_ARN = os.environ["CLUSTER_ARN"]
SECRET_ARN = os.environ["SECRET_ARN"]
AGENT_ID = os.environ["AGENT_ID"]
AGENT_ALIAS_ID = os.environ["AGENT_ALIAS_ID"]
SENDER = os.environ["SENDER"]
RECIPIENTS = [r.strip() for r in os.environ["RECIPIENTS"].split(",") if r.strip()]

SALES_RECENT_SQL = """
SELECT order_id, region, channel, units_sold, unit_price::float AS unit_price,
       revenue::float AS revenue, to_char(sold_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') AS sold_at_utc
FROM product_sales
WHERE sold_at >= NOW() - INTERVAL '24 hours'
ORDER BY sold_at DESC"""

SALES_TOTALS_SQL = """
SELECT
  COUNT(*) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours')                        AS orders_24h,
  COALESCE(SUM(units_sold) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours'), 0)    AS units_24h,
  COALESCE(SUM(revenue) FILTER (WHERE sold_at >= NOW() - INTERVAL '24 hours'), 0)::float AS revenue_24h,
  COUNT(*) FILTER (WHERE sold_at < NOW() - INTERVAL '24 hours')                          AS orders_prior_6d,
  COALESCE(SUM(units_sold) FILTER (WHERE sold_at < NOW() - INTERVAL '24 hours'), 0)     AS units_prior_6d,
  COALESCE(SUM(revenue) FILTER (WHERE sold_at < NOW() - INTERVAL '24 hours'), 0)::float AS revenue_prior_6d
FROM product_sales
WHERE sold_at >= NOW() - INTERVAL '7 days'"""

PROSPECTS_RECENT_SQL = """
SELECT company_name, industry, company_size, interest_level, estimated_budget::float AS estimated_budget,
       source, notes, to_char(submitted_at AT TIME ZONE 'UTC', 'YYYY-MM-DD HH24:MI') AS submitted_at_utc
FROM inbound_prospects
WHERE submitted_at >= NOW() - INTERVAL '24 hours'
ORDER BY submitted_at DESC"""

PROSPECTS_TOTALS_SQL = """
SELECT
  COUNT(*) FILTER (WHERE submitted_at >= NOW() - INTERVAL '24 hours')                                AS submissions_24h,
  COUNT(*) FILTER (WHERE submitted_at >= NOW() - INTERVAL '24 hours' AND interest_level = 'High')    AS high_interest_24h,
  COALESCE(SUM(estimated_budget) FILTER (WHERE submitted_at >= NOW() - INTERVAL '24 hours'), 0)::float AS budget_24h,
  COUNT(*) FILTER (WHERE submitted_at < NOW() - INTERVAL '24 hours')                                  AS submissions_prior_6d,
  COUNT(*) FILTER (WHERE submitted_at < NOW() - INTERVAL '24 hours' AND interest_level = 'High')     AS high_interest_prior_6d,
  COALESCE(SUM(estimated_budget) FILTER (WHERE submitted_at < NOW() - INTERVAL '24 hours'), 0)::float AS budget_prior_6d
FROM inbound_prospects
WHERE submitted_at >= NOW() - INTERVAL '7 days'"""


def query(database, sql, attempts=12):
    """Run a read query via the Data API and return rows as dicts, waiting out an auto-pause resume."""
    for attempt in range(1, attempts + 1):
        try:
            resp = rds_data.execute_statement(
                resourceArn=CLUSTER_ARN,
                secretArn=SECRET_ARN,
                database=database,
                sql=sql,
                formatRecordsAs="JSON",
            )
            return json.loads(resp.get("formattedRecords") or "[]")
        except rds_data.exceptions.DatabaseResumingException:
            logger.info("Database resuming (attempt %d/%d), waiting...", attempt, attempts)
            time.sleep(10)
    raise RuntimeError("Database did not resume in time")


def add_daily_averages(totals):
    """Add *_prior_daily_avg fields so the agent compares like-for-like against the 24h window."""
    for key in [k for k in totals if k.endswith("_prior_6d")]:
        totals[key.replace("_prior_6d", "_prior_daily_avg")] = round(totals[key] / 6, 2)
    return totals


def collect_data():
    return {
        "product_sales": {
            "totals": add_daily_averages(query("sales_db", SALES_TOTALS_SQL)[0]),
            "last_24h_rows": query("sales_db", SALES_RECENT_SQL),
        },
        "inbound_prospects": {
            "totals": add_daily_averages(query("prospects_db", PROSPECTS_TOTALS_SQL)[0]),
            "last_24h_rows": query("prospects_db", PROSPECTS_RECENT_SQL),
        },
    }


def summarize(data):
    """Send the query output to the Bedrock agent and return its two-sentence JSON summary."""
    resp = agent_runtime.invoke_agent(
        agentId=AGENT_ID,
        agentAliasId=AGENT_ALIAS_ID,
        sessionId=str(uuid.uuid4()),
        inputText=(
            "Review this data and produce the daily summary JSON. Compare each *_24h figure against its "
            "*_prior_daily_avg (not the 6-day total).\n" + json.dumps(data, default=str)
        ),
    )
    text = "".join(
        event["chunk"]["bytes"].decode("utf-8") for event in resp["completion"] if "chunk" in event
    )
    logger.info("Agent output: %s", text)
    try:
        parsed = json.loads(text[text.index("{"): text.rindex("}") + 1])
        return parsed["changes_summary"].strip(), parsed["stakeholder_note"].strip()
    except (ValueError, KeyError) as exc:
        raise RuntimeError(f"Agent returned an unexpected response: {text!r}") from exc


def compile_email(data, changes_summary, stakeholder_note, generated_at):
    s = data["product_sales"]["totals"]
    p = data["inbound_prospects"]["totals"]
    lines = [
        "BUSINESS INTELLIGENCE DAILY SUMMARY",
        f"Reporting window: 24 hours ending {generated_at:%Y-%m-%d %H:%M} UTC",
        "",
        f"What changed: {changes_summary}",
        f"Stakeholder note: {stakeholder_note}",
        "",
        "KEY NUMBERS (last 24h | prior 6-day daily average)",
        f"  Sales orders:        {s['orders_24h']} | {s['orders_prior_6d'] / 6:.1f}",
        f"  Units sold:          {s['units_24h']} | {s['units_prior_6d'] / 6:.1f}",
        f"  Revenue:             ${s['revenue_24h']:,.2f} | ${s['revenue_prior_6d'] / 6:,.2f}",
        f"  Prospect submissions: {p['submissions_24h']} | {p['submissions_prior_6d'] / 6:.1f}",
        f"  High-interest leads:  {p['high_interest_24h']} | {p['high_interest_prior_6d'] / 6:.1f}",
        f"  Prospect budget:      ${p['budget_24h']:,.2f} | ${p['budget_prior_6d'] / 6:,.2f}",
        "",
        "SALES LOGGED IN LAST 24H",
    ]
    lines += [
        f"  {r['sold_at_utc']}  {r['order_id']}  {r['region']:<14} {r['channel']:<10} "
        f"{r['units_sold']:>4} units  ${r['revenue']:,.2f}"
        for r in data["product_sales"]["last_24h_rows"]
    ] or ["  (none)"]
    lines += ["", "PROSPECTS SUBMITTED IN LAST 24H"]
    lines += [
        f"  {r['submitted_at_utc']}  {r['company_name']} ({r['industry']}, {r['company_size']}) - "
        f"{r['interest_level']} interest, ${r['estimated_budget']:,.0f}: {r['notes']}"
        for r in data["inbound_prospects"]["last_24h_rows"]
    ] or ["  (none)"]
    lines += ["", "Generated automatically by the business-intelligence-agent service."]
    return "\n".join(lines)


def handler(event, _context):
    logger.info("Triggered by: %s", json.dumps(event))
    generated_at = datetime.now(timezone.utc)

    data = collect_data()
    changes_summary, stakeholder_note = summarize(data)
    body = compile_email(data, changes_summary, stakeholder_note, generated_at)

    summary = {"changesSummary": changes_summary, "stakeholderNote": stakeholder_note, "body": body}
    if event.get("dryRun"):
        logger.info("Dry run: email not sent")
        return {"dryRun": True, **summary}

    subject = f"Daily BI Summary - {generated_at:%Y-%m-%d}"
    resp = sesv2.send_email(
        FromEmailAddress=SENDER,
        Destination={"ToAddresses": RECIPIENTS},
        Content={"Simple": {
            "Subject": {"Data": subject},
            "Body": {"Text": {"Data": body}},
        }},
    )
    logger.info("Sent email %s to %s", resp["MessageId"], RECIPIENTS)
    return {"messageId": resp["MessageId"], "recipients": RECIPIENTS, **summary}
