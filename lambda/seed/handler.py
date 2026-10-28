"""Creates the sales and prospects databases and loads 10 rows of fake data into each.

Runs as a CloudFormation custom resource (via the CDK Provider framework) and can also be
invoked directly with {"RequestType": "Update"} to reseed with fresh relative timestamps.
"""
import logging
import os
import time

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

rds_data = boto3.client("rds-data")
CLUSTER_ARN = os.environ["CLUSTER_ARN"]
SECRET_ARN = os.environ["SECRET_ARN"]

SALES_DDL = """
CREATE TABLE IF NOT EXISTS product_sales (
    id           SERIAL PRIMARY KEY,
    order_id     TEXT NOT NULL,
    product_name TEXT NOT NULL,
    region       TEXT NOT NULL,
    channel      TEXT NOT NULL,
    units_sold   INTEGER NOT NULL,
    unit_price   NUMERIC(10, 2) NOT NULL,
    revenue      NUMERIC(12, 2) GENERATED ALWAYS AS (units_sold * unit_price) STORED,
    sold_at      TIMESTAMPTZ NOT NULL
)"""

# 5 rows in the last 24h, 5 rows 2-7 days old, relative to seed time.
SALES_ROWS = """
INSERT INTO product_sales (order_id, product_name, region, channel, units_sold, unit_price, sold_at) VALUES
 ('ORD-10421', 'NimbusDesk Pro', 'North America', 'Online',      42, 499.00, NOW() - INTERVAL '3 hours'),
 ('ORD-10422', 'NimbusDesk Pro', 'Europe',        'Reseller',    65, 479.00, NOW() - INTERVAL '7 hours'),
 ('ORD-10423', 'NimbusDesk Pro', 'APAC',          'Online',      18, 519.00, NOW() - INTERVAL '11 hours'),
 ('ORD-10424', 'NimbusDesk Pro', 'North America', 'Enterprise', 120, 449.00, NOW() - INTERVAL '16 hours'),
 ('ORD-10425', 'NimbusDesk Pro', 'LATAM',         'Online',       9, 499.00, NOW() - INTERVAL '22 hours'),
 ('ORD-10398', 'NimbusDesk Pro', 'North America', 'Online',      38, 499.00, NOW() - INTERVAL '2 days 4 hours'),
 ('ORD-10387', 'NimbusDesk Pro', 'Europe',        'Reseller',    52, 479.00, NOW() - INTERVAL '3 days 2 hours'),
 ('ORD-10371', 'NimbusDesk Pro', 'APAC',          'Online',      27, 519.00, NOW() - INTERVAL '4 days 6 hours'),
 ('ORD-10355', 'NimbusDesk Pro', 'North America', 'Enterprise',  60, 449.00, NOW() - INTERVAL '5 days 9 hours'),
 ('ORD-10340', 'NimbusDesk Pro', 'LATAM',         'Online',      14, 499.00, NOW() - INTERVAL '6 days 12 hours')
"""

PROSPECTS_DDL = """
CREATE TABLE IF NOT EXISTS inbound_prospects (
    id               SERIAL PRIMARY KEY,
    company_name     TEXT NOT NULL,
    contact_name     TEXT NOT NULL,
    industry         TEXT NOT NULL,
    company_size     TEXT NOT NULL,
    interest_level   TEXT NOT NULL,
    estimated_budget NUMERIC(12, 2) NOT NULL,
    source           TEXT NOT NULL,
    notes            TEXT,
    submitted_at     TIMESTAMPTZ NOT NULL
)"""

PROSPECTS_ROWS = """
INSERT INTO inbound_prospects (company_name, contact_name, industry, company_size, interest_level, estimated_budget, source, notes, submitted_at) VALUES
 ('Brightline Logistics', 'Dana Ortiz',    'Logistics',     '1000+',    'High',   250000, 'Website survey',  'Outfitting 3 new regional offices in Q1',          NOW() - INTERVAL '2 hours'),
 ('Kestrel Health',       'Sam Patel',     'Healthcare',    '200-999',  'High',   180000, 'Webinar survey',  'Needs ADA-compliant desks; asked for demo',        NOW() - INTERVAL '6 hours'),
 ('Orchard Analytics',    'Lee Nakamura',  'Software',      '50-199',   'Medium',  40000, 'Website survey',  'Comparing against two competitors',                NOW() - INTERVAL '9 hours'),
 ('Fenwick & Rowe LLP',   'Priya Shah',    'Legal',         '200-999',  'Low',     15000, 'Trade show form', 'Early research, budget not approved',              NOW() - INTERVAL '14 hours'),
 ('Tidewater Schools',    'Morgan Blake',  'Education',     '1000+',    'High',   320000, 'Website survey',  'District-wide refresh; RFP expected in 30 days',   NOW() - INTERVAL '20 hours'),
 ('Cobalt Fintech',       'Alex Romero',   'Finance',       '50-199',   'Medium',  35000, 'Website survey',  'Hybrid office redesign',                           NOW() - INTERVAL '2 days 3 hours'),
 ('Granite Manufacturing','Jordan Lee',    'Manufacturing', '1000+',    'Low',     20000, 'Trade show form', 'Interested in break-room furniture only',          NOW() - INTERVAL '3 days 5 hours'),
 ('Lumen Studios',        'Casey Kim',     'Media',         '10-49',    'Medium',  12000, 'Webinar survey',  'Small studio expansion',                           NOW() - INTERVAL '4 days 1 hour'),
 ('Northwind Retail',     'Riley Chen',    'Retail',        '200-999',  'Medium',  60000, 'Website survey',  'Corporate HQ floor; timeline 6 months',            NOW() - INTERVAL '5 days 8 hours'),
 ('Summit Biotech',       'Taylor Brooks', 'Biotech',       '50-199',   'High',    90000, 'Website survey',  'Lab offices; asked about volume discounts',        NOW() - INTERVAL '6 days 10 hours')
"""


def execute(sql, database="postgres", parameters=None, attempts=12):
    """Run a statement via the Data API, waiting out an auto-paused cluster resuming."""
    for attempt in range(1, attempts + 1):
        try:
            return rds_data.execute_statement(
                resourceArn=CLUSTER_ARN,
                secretArn=SECRET_ARN,
                database=database,
                sql=sql,
                parameters=parameters or [],
            )
        except rds_data.exceptions.DatabaseResumingException:
            logger.info("Database resuming (attempt %d/%d), waiting...", attempt, attempts)
            time.sleep(10)
    raise RuntimeError("Database did not resume in time")


def ensure_database(name):
    found = execute(
        "SELECT 1 FROM pg_database WHERE datname = :name",
        parameters=[{"name": "name", "value": {"stringValue": name}}],
    )
    if not found["records"]:
        execute(f'CREATE DATABASE "{name}"')
        logger.info("Created database %s", name)


def seed():
    for db, ddl, table, rows in (
        ("sales_db", SALES_DDL, "product_sales", SALES_ROWS),
        ("prospects_db", PROSPECTS_DDL, "inbound_prospects", PROSPECTS_ROWS),
    ):
        ensure_database(db)
        execute(ddl, database=db)
        execute(f"TRUNCATE {table} RESTART IDENTITY", database=db)
        result = execute(rows, database=db)
        logger.info("Seeded %s.%s with %d rows", db, table, result["numberOfRecordsUpdated"])


def handler(event, _context):
    request_type = event.get("RequestType", "Update")
    logger.info("Seed request: %s", request_type)
    if request_type in ("Create", "Update"):
        seed()
    return {"PhysicalResourceId": "bi-agent-seed-data", "Data": {"requestType": request_type}}
