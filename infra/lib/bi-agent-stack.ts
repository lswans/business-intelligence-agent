import * as path from 'path';
import * as crypto from 'crypto';
import * as cdk from 'aws-cdk-lib';
import { Construct } from 'constructs';
import * as ec2 from 'aws-cdk-lib/aws-ec2';
import * as rds from 'aws-cdk-lib/aws-rds';
import * as iam from 'aws-cdk-lib/aws-iam';
import * as lambda from 'aws-cdk-lib/aws-lambda';
import * as logs from 'aws-cdk-lib/aws-logs';
import * as ses from 'aws-cdk-lib/aws-ses';
import * as bedrock from 'aws-cdk-lib/aws-bedrock';
import * as scheduler from 'aws-cdk-lib/aws-scheduler';
import * as cr from 'aws-cdk-lib/custom-resources';

export interface BiAgentStackProps extends cdk.StackProps {
  /** Primary newsletter recipient (verified SES identity). */
  readonly emailAddress: string;
  /** Extra newsletter recipients. Each gets an SES identity (required while SES is in sandbox). */
  readonly additionalRecipients: string[];
  /** From address; must be one of the recipient identities above. Defaults to emailAddress. */
  readonly senderAddress?: string;
  /** Bedrock inference profile ID backing the agent. */
  readonly modelId: string;
}

const LAMBDA_DIR = path.join(__dirname, '..', '..', 'lambda');

const AGENT_INSTRUCTION = `You are a business intelligence analyst writing for an internal daily newsletter.
You will receive JSON containing two datasets: product sales and inbound sales prospects (survey form submissions).
Each dataset has rows logged in the last 24 hours and an aggregate of the prior 6 days for comparison.
Respond with ONLY a JSON object, no markdown fences or extra text, in exactly this shape:
{"changes_summary": "<one sentence summarizing what changed in the last 24 hours versus the prior period, citing concrete numbers>",
 "stakeholder_note": "<one sentence on what stakeholders need to be aware of or act on>"}
Each value must be a single sentence. If a dataset has no rows in the last 24 hours, say so plainly.`;

export class BiAgentStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props: BiAgentStackProps) {
    super(scope, id, props);

    // ---------- Network + database ----------
    // Isolated subnets only: the database is reachable solely through the IAM-authenticated Data API.
    const vpc = new ec2.Vpc(this, 'Vpc', {
      maxAzs: 2,
      natGateways: 0,
      subnetConfiguration: [{ name: 'isolated', subnetType: ec2.SubnetType.PRIVATE_ISOLATED, cidrMask: 24 }],
    });

    const cluster = new rds.DatabaseCluster(this, 'Database', {
      engine: rds.DatabaseClusterEngine.auroraPostgres({
        version: rds.AuroraPostgresEngineVersion.of('16.15', '16'),
      }),
      writer: rds.ClusterInstance.serverlessV2('writer'),
      serverlessV2MinCapacity: 0, // auto-pause when idle
      serverlessV2MaxCapacity: 1,
      enableDataApi: true,
      credentials: rds.Credentials.fromGeneratedSecret('bi_admin'),
      defaultDatabaseName: 'postgres',
      vpc,
      vpcSubnets: { subnetType: ec2.SubnetType.PRIVATE_ISOLATED },
      storageEncrypted: true,
      deletionProtection: false,
      removalPolicy: cdk.RemovalPolicy.DESTROY,
    });
    const secret = cluster.secret!;

    // ---------- Seed data (custom resource; also invokable manually for a reseed) ----------
    const seedFn = new lambda.Function(this, 'SeedFunction', {
      functionName: 'bi-agent-seed-data',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(LAMBDA_DIR, 'seed')),
      timeout: cdk.Duration.minutes(3),
      environment: { CLUSTER_ARN: cluster.clusterArn, SECRET_ARN: secret.secretArn },
      logGroup: new logs.LogGroup(this, 'SeedLogs', {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    cluster.grantDataApiAccess(seedFn);

    const seedProvider = new cr.Provider(this, 'SeedProvider', { onEventHandler: seedFn });
    const seed = new cdk.CustomResource(this, 'SeedData', {
      serviceToken: seedProvider.serviceToken,
      properties: { seedVersion: '3' },
    });
    seed.node.addDependency(cluster);

    // ---------- Bedrock agent ----------
    const modelArns = ['us-east-1', 'us-east-2', 'us-west-2'].map(
      (r) => `arn:aws:bedrock:${r}::foundation-model/${props.modelId.replace(/^us\./, '')}`,
    );
    const inferenceProfileArn = `arn:aws:bedrock:${this.region}:${this.account}:inference-profile/${props.modelId}`;

    const agentRole = new iam.Role(this, 'AgentRole', {
      assumedBy: new iam.ServicePrincipal('bedrock.amazonaws.com', {
        conditions: {
          StringEquals: { 'aws:SourceAccount': this.account },
          ArnLike: { 'aws:SourceArn': `arn:aws:bedrock:${this.region}:${this.account}:agent/*` },
        },
      }),
    });
    agentRole.addToPolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeModel', 'bedrock:InvokeModelWithResponseStream', 'bedrock:GetInferenceProfile'],
      resources: [inferenceProfileArn, ...modelArns],
    }));

    const agent = new bedrock.CfnAgent(this, 'Agent', {
      agentName: 'business-intelligence-agent',
      description: 'Summarizes daily sales and inbound prospect data for the internal newsletter.',
      agentResourceRoleArn: agentRole.roleArn,
      foundationModel: inferenceProfileArn,
      instruction: AGENT_INSTRUCTION,
      idleSessionTtlInSeconds: 600,
      autoPrepare: true,
    });
    // Agent creation validates model access, so the role's inline policy must exist first.
    agent.node.addDependency(agentRole);

    // The alias only picks up a new agent version when it changes, so tie its description to the agent config.
    const agentConfigHash = crypto.createHash('sha256')
      .update(AGENT_INSTRUCTION + props.modelId).digest('hex').slice(0, 12);
    const agentAlias = new bedrock.CfnAgentAlias(this, 'AgentAlias', {
      agentId: agent.attrAgentId,
      agentAliasName: 'live',
      description: `config ${agentConfigHash}`,
    });

    // ---------- Email ----------
    const identity = new ses.EmailIdentity(this, 'EmailIdentity', {
      identity: ses.Identity.email(props.emailAddress),
    });
    const recipientIdentities = props.additionalRecipients.map((address) =>
      new ses.EmailIdentity(this, `RecipientIdentity-${address.replace(/[^A-Za-z0-9]/g, '')}`, {
        identity: ses.Identity.email(address),
      }));
    const recipients = [props.emailAddress, ...props.additionalRecipients];
    const sender = props.senderAddress ?? props.emailAddress;
    if (!recipients.includes(sender)) {
      throw new Error(`senderAddress ${sender} must also be listed as emailAddress or in additionalRecipients`);
    }

    // ---------- Daily report Lambda ----------
    const reportFn = new lambda.Function(this, 'ReportFunction', {
      functionName: 'bi-agent-daily-report',
      description: 'Queries sales + prospect DBs, summarizes with Bedrock agent, emails the newsletter.',
      runtime: lambda.Runtime.PYTHON_3_13,
      handler: 'handler.handler',
      code: lambda.Code.fromAsset(path.join(LAMBDA_DIR, 'report')),
      timeout: cdk.Duration.minutes(2),
      memorySize: 256,
      environment: {
        CLUSTER_ARN: cluster.clusterArn,
        SECRET_ARN: secret.secretArn,
        AGENT_ID: agent.attrAgentId,
        AGENT_ALIAS_ID: agentAlias.attrAgentAliasId,
        SENDER: sender,
        RECIPIENTS: recipients.join(','),
      },
      logGroup: new logs.LogGroup(this, 'ReportLogs', {
        retention: logs.RetentionDays.ONE_MONTH,
        removalPolicy: cdk.RemovalPolicy.DESTROY,
      }),
    });
    cluster.grantDataApiAccess(reportFn);
    reportFn.addToRolePolicy(new iam.PolicyStatement({
      actions: ['bedrock:InvokeAgent'],
      resources: [agentAlias.attrAgentAliasArn],
    }));
    // In the SES sandbox, SendEmail is authorized against the recipient identities as well as the sender.
    [identity, ...recipientIdentities].forEach((i) => i.grantSendEmail(reportFn));

    // ---------- Schedule: 08:00 US Eastern daily ----------
    const schedulerRole = new iam.Role(this, 'SchedulerRole', {
      assumedBy: new iam.ServicePrincipal('scheduler.amazonaws.com', {
        conditions: { StringEquals: { 'aws:SourceAccount': this.account } },
      }),
    });
    reportFn.grantInvoke(schedulerRole);

    new scheduler.CfnSchedule(this, 'DailySchedule', {
      name: 'bi-agent-daily-report',
      description: 'Triggers the BI newsletter at 08:00 America/New_York.',
      scheduleExpression: 'cron(0 8 * * ? *)',
      scheduleExpressionTimezone: 'America/New_York',
      flexibleTimeWindow: { mode: 'OFF' },
      state: 'ENABLED',
      target: {
        arn: reportFn.functionArn,
        roleArn: schedulerRole.roleArn,
        input: JSON.stringify({ source: 'eventbridge-scheduler' }),
        retryPolicy: { maximumRetryAttempts: 2, maximumEventAgeInSeconds: 3600 },
      },
    });

    // ---------- Outputs ----------
    new cdk.CfnOutput(this, 'ReportFunctionName', { value: reportFn.functionName });
    new cdk.CfnOutput(this, 'ManualTriggerConsoleUrl', {
      value: `https://${this.region}.console.aws.amazon.com/lambda/home?region=${this.region}#/functions/${reportFn.functionName}?tab=testing`,
    });
    new cdk.CfnOutput(this, 'ClusterArn', { value: cluster.clusterArn });
    new cdk.CfnOutput(this, 'AgentId', { value: agent.attrAgentId });
    new cdk.CfnOutput(this, 'AgentAliasId', { value: agentAlias.attrAgentAliasId });
  }
}
