#!/usr/bin/env node
import * as fs from 'fs';
import * as path from 'path';
import * as cdk from 'aws-cdk-lib';
import { BiAgentStack } from '../lib/bi-agent-stack';

interface EmailConfig {
  emailAddress: string;
  additionalRecipients?: string[];
  senderAddress?: string;
}

// Email addresses are personal, so they live in a gitignored config.json (see config.example.json).
const configPath = path.join(__dirname, '..', 'config.json');
if (!fs.existsSync(configPath)) {
  throw new Error('Missing infra/config.json: copy infra/config.example.json to infra/config.json and set your email addresses.');
}
const config: EmailConfig = JSON.parse(fs.readFileSync(configPath, 'utf8'));

const app = new cdk.App();

new BiAgentStack(app, 'BusinessIntelligenceAgent', {
  // Account comes from the active AWS credentials; region is pinned in cdk.json (not AWS_REGION).
  env: { account: process.env.CDK_DEFAULT_ACCOUNT, region: app.node.getContext('region') },
  emailAddress: config.emailAddress,
  additionalRecipients: config.additionalRecipients ?? [],
  senderAddress: config.senderAddress,
  modelId: app.node.getContext('modelId'),
});

cdk.Tags.of(app).add('Project', 'business-intelligence-agent');
cdk.Tags.of(app).add('Environment', 'dev');
