#!/bin/bash

set -e

# Configuration variables
STACK_NAME="fraud-detection-durable-function"
FUNCTION_NAME="fn-Fraud-Detection"
ROLE_NAME="durable-function-execution-role"
AGENT_RUNTIME_NAME="fraud_risk_scorer"
AGENT_ROLE_NAME="bedrock-agentcore-runtime-fraud-role"
ECR_REPO_NAME="fraud-risk-scorer"
LAMBDA_REGION="us-east-1"
AGENT_REGION="us-east-1"

echo "🚀 Deploying Fraud Detection Durable Function using SAM (Python)"
echo "================================================================"
echo ""

# Get AWS Account ID
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "📋 Account ID: $ACCOUNT_ID"
echo "📋 Lambda Region: $LAMBDA_REGION"
echo "📋 Agent Region: $AGENT_REGION"
echo ""

# Check prerequisites
echo "🔍 Checking prerequisites..."

if ! command -v sam &> /dev/null; then
    echo "❌ SAM CLI is not installed. Please install it first:"
    echo "   https://docs.aws.amazon.com/serverless-application-model/latest/developerguide/install-sam-cli.html"
    exit 1
fi

if ! command -v python3 &> /dev/null; then
    echo "❌ Python3 is not installed."
    exit 1
fi

if ! docker info &> /dev/null; then
    echo "❌ Docker is not running. Please start Docker Desktop."
    exit 1
fi

echo "✅ Prerequisites checked"
echo ""

# Build and push Docker image for agent
echo "🐳 Building and pushing agent Docker image..."
cd FraudDetection-Agent

ECR_URI="$ACCOUNT_ID.dkr.ecr.$AGENT_REGION.amazonaws.com/$ECR_REPO_NAME"

echo "📦 Ensuring ECR repository exists..."
if ! aws ecr describe-repositories --repository-names $ECR_REPO_NAME --region $AGENT_REGION >/dev/null 2>&1; then
    echo "📝 Creating ECR repository: $ECR_REPO_NAME"
    aws ecr create-repository --repository-name $ECR_REPO_NAME --region $AGENT_REGION >/dev/null
fi

echo "🔐 Logging in to ECR..."
aws ecr get-login-password --region $AGENT_REGION | docker login --username AWS --password-stdin $ECR_URI

echo "🏗️ Building and pushing Docker image..."
docker buildx create --use --name sam-builder 2>/dev/null || docker buildx use sam-builder
docker buildx build --platform linux/arm64 -t $ECR_URI:latest --push .

echo "✅ Docker image pushed to ECR"
cd ..

# Build Python Lambda function with bundled dependencies
echo "📦 Building Python Lambda function..."
cd FraudDetection-Lambda

echo "🧹 Cleaning directory..."
rm -rf package function.zip || true

echo "📥 Installing Python dependencies into package/..."
pip install -r requirements.txt --target package/ --break-system-packages --quiet

echo "📦 Creating function package..."
# Copy source code into package dir
cp app.py package/

# Zip everything from inside package/
cd package
zip -qr ../function.zip .
cd ..

echo "✅ Function package created"

# Ensure S3 bucket exists
BUCKET_NAME="durable-functions-$ACCOUNT_ID"
echo "📦 Ensuring S3 bucket exists: $BUCKET_NAME"

if ! aws s3api head-bucket --bucket $BUCKET_NAME --region $LAMBDA_REGION 2>/dev/null; then
    echo "📝 Creating S3 bucket: $BUCKET_NAME"
    if [ "$LAMBDA_REGION" = "us-east-1" ]; then
        aws s3api create-bucket --bucket $BUCKET_NAME --region $LAMBDA_REGION
    else
        aws s3api create-bucket --bucket $BUCKET_NAME --region $LAMBDA_REGION \
            --create-bucket-configuration LocationConstraint=$LAMBDA_REGION
    fi

    aws s3api put-bucket-versioning --bucket $BUCKET_NAME \
        --versioning-configuration Status=Enabled --region $LAMBDA_REGION

    aws s3api put-bucket-encryption --bucket $BUCKET_NAME --region $LAMBDA_REGION \
        --server-side-encryption-configuration '{
            "Rules": [{"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}]
        }'

    aws s3api put-public-access-block --bucket $BUCKET_NAME --region $LAMBDA_REGION \
        --public-access-block-configuration \
        BlockPublicAcls=true,IgnorePublicAcls=true,BlockPublicPolicy=true,RestrictPublicBuckets=true
fi

echo "⬆️  Uploading function package to S3..."
aws s3 cp function.zip s3://$BUCKET_NAME/functions/$FUNCTION_NAME.zip --region $LAMBDA_REGION

echo "🧹 Cleaning up build artifacts..."
rm -rf package function.zip

cd ..

# Deploy SAM application
echo "🚀 Deploying SAM stack..."
sam deploy \
    --stack-name $STACK_NAME \
    --s3-bucket $BUCKET_NAME \
    --s3-prefix sam-artifacts \
    --capabilities CAPABILITY_NAMED_IAM \
    --region $LAMBDA_REGION \
    --parameter-overrides \
        FunctionName=$FUNCTION_NAME \
        RoleName=$ROLE_NAME \
        AgentRuntimeName=$AGENT_RUNTIME_NAME \
        AgentRoleName=$AGENT_ROLE_NAME \
        ECRRepoName=$ECR_REPO_NAME \
        LambdaRegion=$LAMBDA_REGION \
        AgentRegion=$AGENT_REGION \
    --no-fail-on-empty-changeset

echo ""
echo "⏳ Waiting for stack deployment to complete..."

aws cloudformation wait stack-update-complete --stack-name $STACK_NAME --region $LAMBDA_REGION 2>/dev/null || \
aws cloudformation wait stack-create-complete --stack-name $STACK_NAME --region $LAMBDA_REGION

# Get outputs
echo "📋 Retrieving stack outputs..."
OUTPUTS=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --region $LAMBDA_REGION \
    --query 'Stacks[0].Outputs')

FUNCTION_ARN=$(echo $OUTPUTS | jq -r '.[] | select(.OutputKey=="LambdaFunctionArn") | .OutputValue')
AGENT_RUNTIME_ARN=$(echo $OUTPUTS | jq -r '.[] | select(.OutputKey=="AgentRuntimeArn") | .OutputValue')

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Deployment Summary:"
echo "   Stack Name:        $STACK_NAME"
echo "   Function Name:     $FUNCTION_NAME"
echo "   Function ARN:      $FUNCTION_ARN"
echo "   Agent Runtime ARN: $AGENT_RUNTIME_ARN"
echo "   S3 Bucket:         $BUCKET_NAME"
echo "   Lambda Region:     $LAMBDA_REGION"
echo "   Agent Region:      $AGENT_REGION"
echo ""

echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "💰 MANUAL TESTING - Transaction Scenarios"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1️⃣  Low Risk Transaction (Auto-Approve, score < 3):"
echo "   aws lambda invoke \\"
echo "     --function-name '$FUNCTION_NAME:\$LATEST' \\"
echo "     --invocation-type Event \\"
echo "     --cli-binary-format raw-in-base64-out \\"
echo "     --payload '{\"id\":1,\"amount\":500,\"location\":\"New York\",\"vendor\":\"Amazon\"}' \\"
echo "     --region $LAMBDA_REGION \\"
echo "     output-low-risk.json"
echo ""
echo "2️⃣  High Risk Transaction (Send to Fraud, score = 5):"
echo "   aws lambda invoke \\"
echo "     --function-name '$FUNCTION_NAME:\$LATEST' \\"
echo "     --invocation-type Event \\"
echo "     --cli-binary-format raw-in-base64-out \\"
echo "     --payload '{\"id\":2,\"amount\":10000,\"location\":\"Unknown\",\"vendor\":\"Suspicious Store\"}' \\"
echo "     --region $LAMBDA_REGION \\"
echo "     output-high-risk.json"
echo ""
echo "3️⃣  Medium Risk Transaction (Human Verification, score 3-4):"
echo "   aws lambda invoke \\"
echo "     --function-name '$FUNCTION_NAME:\$LATEST' \\"
echo "     --invocation-type Event \\"
echo "     --cli-binary-format raw-in-base64-out \\"
echo "     --payload '{\"id\":3,\"amount\":6500,\"location\":\"Los Angeles\",\"vendor\":\"Electronics Store\"}' \\"
echo "     --region $LAMBDA_REGION \\"
echo "     output-medium-risk.json"
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 MONITORING & DEBUGGING"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📝 View CloudWatch Logs:"
echo "   aws logs tail /aws/lambda/$FUNCTION_NAME \\"
echo "     --region $LAMBDA_REGION \\"
echo "     --follow"
echo ""
echo "🗑️  To delete the stack:"
echo "   sam delete --stack-name $STACK_NAME --region $LAMBDA_REGION"
echo ""
echo "✅ SAM deployment complete!"