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

# Available templates
TEMPLATE_WITH_AGENT="template-with-agent-bedrock.yaml"
TEMPLATE_WITHOUT_AGENT="template-without-agent-bedrock.yaml"

echo "🚀 Fraud Detection Durable Function — SAM Deployment (Python)"
echo "=============================================================="
echo ""

# ── Template selection ────────────────────────────────────────────────────────
echo "📄 Select the deployment template:"
echo ""
echo "  [1] With Bedrock Agent    ($TEMPLATE_WITH_AGENT)"
echo "      Deploys the Lambda function + Bedrock AgentCore runtime."
echo "      Requires Docker to build and push the agent container image."
echo ""
echo "  [2] Without Bedrock Agent ($TEMPLATE_WITHOUT_AGENT)"
echo "      Deploys the Lambda function only."
echo "      No Docker or ECR steps are performed."
echo ""

while true; do
    read -rp "Enter your choice [1/2]: " TEMPLATE_CHOICE
    case "$TEMPLATE_CHOICE" in
        1)
            SAM_TEMPLATE="$TEMPLATE_WITH_AGENT"
            DEPLOY_AGENT=true
            echo ""
            echo "✅ Selected: With Bedrock Agent ($SAM_TEMPLATE)"
            break
            ;;
        2)
            SAM_TEMPLATE="$TEMPLATE_WITHOUT_AGENT"
            DEPLOY_AGENT=false
            echo ""
            echo "✅ Selected: Without Bedrock Agent ($SAM_TEMPLATE)"
 
            # Prompt for the external agent base URL (required for this template)
            while true; do
                read -rp "🌐 Enter the AGENT_BASE_URL (e.g. https://my-agent.example.com): " AGENT_BASE_URL
                if [ -z "$AGENT_BASE_URL" ]; then
                    echo "❌ AGENT_BASE_URL cannot be empty. Please provide a valid URL."
                elif [[ ! "$AGENT_BASE_URL" =~ ^https?:// ]]; then
                    echo "❌ AGENT_BASE_URL must start with http:// or https://"
                else
                    echo "✅ AGENT_BASE_URL set to: $AGENT_BASE_URL"
                    break
                fi
            done
            break
            ;;
        *)
            echo "❌ Invalid choice. Please enter 1 or 2."
            ;;
    esac
done
 
echo ""

# Validate that the selected template file exists
if [ ! -f "$SAM_TEMPLATE" ]; then
    echo "❌ Template file not found: $SAM_TEMPLATE"
    echo "   Make sure the file exists in the current directory."
    exit 1
fi

# ── AWS Account ID ────────────────────────────────────────────────────────────
ACCOUNT_ID=$(aws sts get-caller-identity --query Account --output text)
echo "📋 Account ID:     $ACCOUNT_ID"
echo "📋 Lambda Region:  $LAMBDA_REGION"
if [ "$DEPLOY_AGENT" = true ]; then
    echo "📋 Agent Region:   $AGENT_REGION"
else
    echo "📋 Agent Base URL: $AGENT_BASE_URL"
fi
echo "📋 Template:       $SAM_TEMPLATE"
echo ""

# ── Prerequisites ─────────────────────────────────────────────────────────────
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

# Docker is only required when deploying with the Bedrock Agent template
if [ "$DEPLOY_AGENT" = true ]; then
    if ! docker info &> /dev/null; then
        echo "❌ Docker is not running. Please start Docker Desktop."
        exit 1
    fi
    echo "✅ Prerequisites checked (SAM CLI, Python3, Docker)"
else
    echo "✅ Prerequisites checked (SAM CLI, Python3)"
fi
echo ""

# ── Unit tests ────────────────────────────────────────────────────────────────
echo "🧪 Running FraudDetection-Lambda tests..."
cd FraudDetection-Lambda

# For test (don't change)
export USE_BEDROCK_AGENTCORE="true"

pip install pytest --quiet --break-system-packages 2>/dev/null

if ! python3 -m pytest test_app.py -v; then
    echo "❌ Tests failed. Aborting deployment."
    exit 1
fi

echo "✅ All tests passed"
echo ""
cd ..

# ── Docker / ECR (only with Bedrock Agent template) ───────────────────────────
if [ "$DEPLOY_AGENT" = true ]; then
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

    echo "🏗️  Building and pushing Docker image..."
    docker buildx create --use --name sam-builder 2>/dev/null || docker buildx use sam-builder
    docker buildx build --platform linux/arm64 -t $ECR_URI:latest --push .

    # Capture image digest so CloudFormation detects changes on re-deploy
    IMAGE_DIGEST=$(aws ecr describe-images \
        --repository-name $ECR_REPO_NAME \
        --region $AGENT_REGION \
        --image-ids imageTag=latest \
        --query 'imageDetails[0].imageDigest' \
        --output text)
    echo "📋 Image digest: $IMAGE_DIGEST"

    echo "✅ Docker image pushed to ECR"
    cd ..
else
    echo "⏭️  Skipping Docker/ECR steps (template does not include Bedrock Agent)"
    echo ""
fi

# ── Lambda package ────────────────────────────────────────────────────────────
echo "📦 Building Python Lambda function..."
cd FraudDetection-Lambda

echo "🧹 Cleaning previous build artifacts..."
rm -rf package function.zip || true

echo "📥 Installing Python dependencies into package/..."
pip install -r requirements.txt --target package/ --break-system-packages --quiet

echo "📦 Creating function package..."
cp app.py package/
cd package
zip -qr ../function.zip .
cd ..

echo "✅ Function package created"

# ── S3 bucket ─────────────────────────────────────────────────────────────────
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
CODE_VERSION=$(aws s3api put-object \
    --bucket $BUCKET_NAME \
    --key "functions/$FUNCTION_NAME.zip" \
    --body function.zip \
    --region $LAMBDA_REGION \
    --query 'VersionId' \
    --output text)
echo "📋 Code S3 version: $CODE_VERSION"

echo "🧹 Cleaning up build artifacts..."
rm -rf package function.zip

cd ..

# ── SAM deploy ────────────────────────────────────────────────────────────────
echo ""
echo "🚀 Deploying SAM stack with template: $SAM_TEMPLATE"

# Build the parameter overrides list; agent-specific params are only added
# when the Bedrock Agent template is selected.
PARAM_OVERRIDES=(
    "FunctionName=$FUNCTION_NAME"
    "RoleName=$ROLE_NAME"
    "LambdaRegion=$LAMBDA_REGION"
    "CodeVersion=$CODE_VERSION"
)
 
if [ "$DEPLOY_AGENT" = true ]; then
    PARAM_OVERRIDES+=(
        "AgentRuntimeName=$AGENT_RUNTIME_NAME"
        "AgentRoleName=$AGENT_ROLE_NAME"
        "ECRRepoName=$ECR_REPO_NAME"
        "AgentRegion=$AGENT_REGION"
        "ImageDigest=$IMAGE_DIGEST"
    )
else
    PARAM_OVERRIDES+=(
        "AgentBaseUrl=$AGENT_BASE_URL"
    )
fi

sam deploy \
    --template-file "$SAM_TEMPLATE" \
    --stack-name $STACK_NAME \
    --s3-bucket $BUCKET_NAME \
    --s3-prefix sam-artifacts \
    --capabilities CAPABILITY_NAMED_IAM \
    --region $LAMBDA_REGION \
    --parameter-overrides "${PARAM_OVERRIDES[@]}" \
    --no-fail-on-empty-changeset

# ── Stack outputs ─────────────────────────────────────────────────────────────
echo "📋 Retrieving stack outputs..."
OUTPUTS=$(aws cloudformation describe-stacks \
    --stack-name $STACK_NAME \
    --region $LAMBDA_REGION \
    --query 'Stacks[0].Outputs')

FUNCTION_ARN=$(echo $OUTPUTS | jq -r '.[] | select(.OutputKey=="LambdaFunctionArn") | .OutputValue')

echo ""
echo "✅ Deployment complete!"
echo ""
echo "📋 Deployment Summary:"
echo "   Stack Name:    $STACK_NAME"
echo "   Template:      $SAM_TEMPLATE"
echo "   Function Name: $FUNCTION_NAME"
echo "   Function ARN:  $FUNCTION_ARN"
echo "   S3 Bucket:     $BUCKET_NAME"
echo "   Lambda Region: $LAMBDA_REGION"

if [ "$DEPLOY_AGENT" = true ]; then
    AGENT_RUNTIME_ARN=$(echo $OUTPUTS | jq -r '.[] | select(.OutputKey=="AgentRuntimeArn") | .OutputValue')
    echo "   Agent ARN:     $AGENT_RUNTIME_ARN"
    echo "   Agent Region:  $AGENT_REGION"
fi

# ── Manual testing commands ───────────────────────────────────────────────────
echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "💰 MANUAL TESTING — Transaction Scenarios"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "1️⃣  Low Risk Transaction (Auto-Approve, score < 3):"
echo "   aws lambda invoke \\"
echo "     --function-name '$FUNCTION_NAME:\$LATEST' \\"
echo "     --invocation-type Event \\"
echo "     --cli-binary-format raw-in-base64-out \\"
echo "     --payload '{\"id\":1,\"amount\":500,\"location\":\"New York\",\"vendor\":\"Amazon\"}' \\"
echo "     --region $LAMBDA_REGION \\"
echo "     /dev/null"
echo ""
echo "2️⃣  High Risk Transaction (Send to Fraud, score = 5):"
echo "   aws lambda invoke \\"
echo "     --function-name '$FUNCTION_NAME:\$LATEST' \\"
echo "     --invocation-type Event \\"
echo "     --cli-binary-format raw-in-base64-out \\"
echo "     --payload '{\"id\":2,\"amount\":10000,\"location\":\"Las Vegas\",\"vendor\":\"crypto\"}' \\"
echo "     --region $LAMBDA_REGION \\"
echo "     /dev/null"
echo ""
echo "3️⃣  Medium Risk Transaction (Human Verification, score 3-4):"
echo "   aws lambda invoke \\"
echo "     --function-name '$FUNCTION_NAME:\$LATEST' \\"
echo "     --invocation-type Event \\"
echo "     --cli-binary-format raw-in-base64-out \\"
echo "     --payload '{\"id\":3,\"amount\":1500,\"location\":\"Barcelona\",\"vendor\":\"Electronics Store\"}' \\"
echo "     --region $LAMBDA_REGION \\"
echo "     /dev/null"
echo ""

# ── Monitoring ────────────────────────────────────────────────────────────────
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "📊 MONITORING & DEBUGGING"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo ""
echo "📝 View CloudWatch Logs:"
echo "   aws logs tail /aws/lambda/$FUNCTION_NAME \\"
echo "     --region $LAMBDA_REGION \\"
echo "     --follow"
echo ""
echo "🗑️  Delete the stack:"
echo "   sam delete --stack-name $STACK_NAME --region $LAMBDA_REGION"
echo ""
echo "✅ SAM deployment complete!"