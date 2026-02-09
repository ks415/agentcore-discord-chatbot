import * as path from "path";
import * as cdk from "aws-cdk-lib";
import * as apigateway from "aws-cdk-lib/aws-apigateway";
import * as dynamodb from "aws-cdk-lib/aws-dynamodb";
import * as events from "aws-cdk-lib/aws-events";
import * as targets from "aws-cdk-lib/aws-events-targets";
import * as iam from "aws-cdk-lib/aws-iam";
import * as lambda from "aws-cdk-lib/aws-lambda";
import * as agentcore from "@aws-cdk/aws-bedrock-agentcore-alpha";
import { Construct } from "constructs";

export class AgentcoreLineChatbotStack extends cdk.Stack {
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
        LINE_CHANNEL_SECRET: process.env.LINE_CHANNEL_SECRET || "",
        LINE_CHANNEL_ACCESS_TOKEN: process.env.LINE_CHANNEL_ACCESS_TOKEN || "",
        AGENTCORE_RUNTIME_ARN: runtime.agentRuntimeArn,
      },
    });

    // Lambda → AgentCore 呼び出し権限
    runtime.grantInvokeRuntime(webhookFn);

    // ========================================
    // API Gateway (REST API - 非同期 Lambda 呼び出し)
    // ========================================
    const api = new apigateway.RestApi(this, "WebhookApi", {
      restApiName: "agentcore-line-chatbot-webhook",
      description: "LINE Webhook endpoint for AgentCore LINE Chatbot",
    });

    // API Gateway → Lambda 非同期呼び出し用ロール
    const apiGatewayRole = new iam.Role(this, "ApiGatewayLambdaRole", {
      assumedBy: new iam.ServicePrincipal("apigateway.amazonaws.com"),
    });
    webhookFn.grantInvoke(apiGatewayRole);

    // Lambda 非同期呼び出し統合
    const lambdaIntegration = new apigateway.AwsIntegration({
      service: "lambda",
      path: `2015-03-31/functions/${webhookFn.functionArn}/invocations`,
      integrationHttpMethod: "POST",
      options: {
        credentialsRole: apiGatewayRole,
        requestParameters: {
          "integration.request.header.X-Amz-Invocation-Type": "'Event'",
        },
        requestTemplates: {
          "application/json": `{
  "body": "$util.escapeJavaScript($input.body)",
  "signature": "$input.params('x-line-signature')"
}`,
        },
        integrationResponses: [
          {
            statusCode: "200",
            responseTemplates: {
              "application/json": '{"message": "accepted"}',
            },
          },
        ],
      },
    });

    const webhook = api.root.addResource("webhook");
    webhook.addMethod("POST", lambdaIntegration, {
      methodResponses: [{ statusCode: "200" }],
    });

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
        LINE_CHANNEL_ACCESS_TOKEN: process.env.LINE_CHANNEL_ACCESS_TOKEN || "",
        LINE_NOTIFY_TO: process.env.LINE_NOTIFY_TO || "",
        RACER_NO: process.env.RACER_NO || "3941",
        DYNAMODB_TABLE: predictionTable.tableName,
      },
    });

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

    // EventBridge Rule: 毎朝 JST 8:00 (= UTC 23:00 前日) に予想生成
    const morningRule = new events.Rule(this, "MorningScraperRule", {
      ruleName: "boat-race-morning-prediction",
      schedule: events.Schedule.cron({
        minute: "0",
        hour: "23",
        day: "*",
        month: "*",
        year: "*",
      }),
      description: "毎日 JST 8:00 に出走予定取得＋AI予想生成",
    });
    morningRule.addTarget(
      new targets.LambdaFunction(scraperFn, {
        event: events.RuleTargetInput.fromObject({ mode: "morning" }),
      }),
    );

    // EventBridge Rule: 毎晩 JST 22:00 (= UTC 13:00) に結果収集
    const eveningRule = new events.Rule(this, "EveningScraperRule", {
      ruleName: "boat-race-evening-result",
      schedule: events.Schedule.cron({
        minute: "0",
        hour: "13",
        day: "*",
        month: "*",
        year: "*",
      }),
      description: "毎日 JST 22:00 にレース結果収集＋収支計算",
    });
    eveningRule.addTarget(
      new targets.LambdaFunction(scraperFn, {
        event: events.RuleTargetInput.fromObject({ mode: "evening" }),
      }),
    );

    // ========================================
    // Outputs
    // ========================================
    new cdk.CfnOutput(this, "WebhookUrl", {
      value: `${api.url}webhook`,
      description: "LINE Webhook URL",
    });

    new cdk.CfnOutput(this, "AgentRuntimeArn", {
      value: runtime.agentRuntimeArn,
      description: "AgentCore Runtime ARN",
    });
  }
}
