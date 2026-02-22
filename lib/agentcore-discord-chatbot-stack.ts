import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as scheduler from "aws-cdk-lib/aws-scheduler";
import * as agentcore from "@aws-cdk/aws-bedrock-agentcore-alpha";
import { Construct } from "constructs";

export class AgentcoreDiscordChatbotStack extends cdk.Stack {
  constructor(scope: Construct, id: string, props?: cdk.StackProps) {
    super(scope, id, props);

    // ========================================
    // AgentCore Runtime
    // ========================================
    const runtime = new agentcore.Runtime(this, "ChatbotAgentRuntime", {
      runtimeName: "agentcore_line_chatbot",
      agentRuntimeArtifact: agentcore.AgentRuntimeArtifact.fromAsset(
        path.join(__dirname, "../agent"),
      ),
      networkConfiguration:
        agentcore.RuntimeNetworkConfiguration.usingPublicNetwork(),
      environmentVariables: {
        TAVILY_API_KEY: process.env.TAVILY_API_KEY || "",
        AGENT_OBSERVABILITY_ENABLED: "true",
        OTEL_PYTHON_DISTRO: "aws_distro",
        OTEL_PYTHON_CONFIGURATOR: "aws_configurator",
        OTEL_EXPORTER_OTLP_PROTOCOL: "http/protobuf",
      },
    });

    // Bedrock モデル呼び出し権限
    runtime.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "bedrock:InvokeModel",
          "bedrock:InvokeModelWithResponseStream",
        ],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:*:inference-profile/*",
        ],
      }),
    );

    // ========================================
    // Lambda (Webhook Handler + SSE Bridge)
    // ========================================
    const webhookFn = new lambda.Function(this, "WebhookFunction", {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: "webhook.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda"), {
        bundling: {
          image: lambda.Runtime.PYTHON_3_13.bundlingImage,
          platform: "linux/arm64",
          command: [
            "bash",
            "-c",
            "pip install -r requirements.txt -t /asset-output && cp *.py /asset-output",
          ],
        },
      }),
      timeout: cdk.Duration.seconds(120),
      memorySize: 256,
      environment: {
        DISCORD_PUBLIC_KEY: process.env.DISCORD_PUBLIC_KEY || "",
        DISCORD_APPLICATION_ID: process.env.DISCORD_APPLICATION_ID || "",
        AGENTCORE_RUNTIME_ARN: runtime.agentRuntimeArn,
      },
    });

    // Lambda → AgentCore 呼び出し権限
    runtime.grantInvokeRuntime(webhookFn);

    // Lambda → 自分自身を非同期呼び出し（Discord deferred response 用）
    webhookFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: ["*"],
      }),
    );

    // ========================================
    // API Gateway (REST API - Discord Interactions Endpoint)
    // ========================================
    const api = new apigateway.RestApi(this, "WebhookApi", {
      restApiName: "agentcore-discord-chatbot-webhook",
      description:
        "Discord Interactions endpoint for AgentCore Discord Chatbot",
    });

    // Discord Interactions Endpoint: 同期 Lambda 統合（署名検証 + deferred response を Lambda が返す）
    const lambdaIntegration = new apigateway.LambdaIntegration(webhookFn);

    const webhook = api.root.addResource("webhook");
    webhook.addMethod("POST", lambdaIntegration);

    // ========================================
    // DynamoDB (予想・結果・累計収支)
    // ========================================
    const predictionTable = new dynamodb.Table(this, "PredictionTable", {
      tableName: "BoatRacePredictions",
      partitionKey: { name: "racer_no", type: dynamodb.AttributeType.STRING },
      sortKey: { name: "date_type", type: dynamodb.AttributeType.STRING },
      billingMode: dynamodb.BillingMode.PAY_PER_REQUEST,
      removalPolicy: cdk.RemovalPolicy.RETAIN,
    });

    // ========================================
    // EventBridge Scheduler 用 IAM ロール
    // (動的に作成される one-time schedule が Scraper Lambda を呼び出す)
    // ========================================
    const schedulerRole = new iam.Role(this, "SchedulerRole", {
      roleName: "boat-race-scheduler-role",
      assumedBy: new iam.ServicePrincipal("scheduler.amazonaws.com"),
    });

    // ========================================
    // EventBridge Scheduler グループ
    // (レースごとの one-time schedule をまとめる名前空間)
    // ========================================
    const schedulerGroup = new scheduler.CfnScheduleGroup(
      this,
      "SchedulerGroup",
      {
        name: "boat-race-schedules",
      },
    );

    // ========================================
    // Lambda (Scraper - 予想＋収支管理)
    // ========================================
    const scraperFn = new lambda.Function(this, "ScraperFunction", {
      runtime: lambda.Runtime.PYTHON_3_13,
      architecture: lambda.Architecture.ARM_64,
      handler: "scraper.handler",
      code: lambda.Code.fromAsset(path.join(__dirname, "../lambda"), {
        bundling: {
          image: lambda.Runtime.PYTHON_3_13.bundlingImage,
          platform: "linux/arm64",
          command: [
            "bash",
            "-c",
            "pip install -r requirements.txt -t /asset-output && cp *.py /asset-output",
          ],
        },
      }),
      timeout: cdk.Duration.seconds(180),
      memorySize: 512,
      environment: {
        DISCORD_WEBHOOK_URL: process.env.DISCORD_WEBHOOK_URL || "",
        RACER_NO: process.env.RACER_NO || "3941",
        DYNAMODB_TABLE: predictionTable.tableName,
        SCHEDULER_ROLE_ARN: schedulerRole.roleArn,
        SCHEDULER_GROUP_NAME: schedulerGroup.name!,
      },
    });

    // SCRAPER_FUNCTION_ARN は Lambda 実行時に context.invoked_function_arn から取得
    // （CDK で scraperFn.functionArn を自身の環境変数に設定すると CloudFormation の循環参照になるため）

    // Scraper → DynamoDB 読み書き権限
    predictionTable.grantReadWriteData(scraperFn);

    // Scraper → Bedrock モデル呼び出し権限
    scraperFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["bedrock:InvokeModel"],
        resources: [
          "arn:aws:bedrock:*::foundation-model/*",
          "arn:aws:bedrock:*:*:inference-profile/*",
        ],
      }),
    );
    // クロスリージョン推論プロファイル利用に必要な Marketplace 権限
    scraperFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "aws-marketplace:ViewSubscriptions",
          "aws-marketplace:Subscribe",
        ],
        resources: ["*"],
      }),
    );

    // Scraper → EventBridge Scheduler 操作権限
    scraperFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: [
          "scheduler:CreateSchedule",
          "scheduler:DeleteSchedule",
          "scheduler:GetSchedule",
        ],
        resources: [
          `arn:aws:scheduler:${this.region}:${this.account}:schedule/boat-race-schedules/*`,
        ],
      }),
    );

    // Scraper → iam:PassRole (Scheduler 用ロールを渡す権限)
    scraperFn.addToRolePolicy(
      new iam.PolicyStatement({
        actions: ["iam:PassRole"],
        resources: [schedulerRole.roleArn],
      }),
    );

    // Scheduler ロール → Scraper Lambda 呼び出し権限
    // NOTE: scraperFn.functionArn を直接参照すると ScraperFunction ↔ SchedulerRole の
    //       循環参照になるため、パターンで指定して依存を断つ
    schedulerRole.addToPolicy(
      new iam.PolicyStatement({
        actions: ["lambda:InvokeFunction"],
        resources: [
          `arn:aws:lambda:${this.region}:${this.account}:function:AgentcoreDiscordChatbot*`,
        ],
      }),
    );

    // EventBridge Rule: 毎朝 JST 8:00 (= UTC 23:00 前日) にスケジュール生成
    const morningRule = new events.Rule(this, "MorningScraperRule", {
      ruleName: "boat-race-morning-schedule",
      schedule: events.Schedule.cron({
        minute: "0",
        hour: "23",
        day: "*",
        month: "*",
        year: "*",
      }),
      description:
        "毎日 JST 8:00 に出走予定取得＋レースごとの動的スケジュール作成",
    });
    morningRule.addTarget(
      new targets.LambdaFunction(scraperFn, {
        event: events.RuleTargetInput.fromObject({ mode: "schedule" }),
      }),
    );

    // ========================================
    // Outputs
    // ========================================
    new cdk.CfnOutput(this, "WebhookUrl", {
      value: `${api.url}webhook`,
      description: "Discord Interactions Endpoint URL",
    });

    new cdk.CfnOutput(this, "AgentRuntimeArn", {
      value: runtime.agentRuntimeArn,
      description: "AgentCore Runtime ARN",
    });
  }
}
