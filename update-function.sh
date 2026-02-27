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

echo "⬆️  Updating Lambda function..."
aws lambda update-function-code \
    --function-name $FUNCTION_NAME \
    --zip-file fileb://function.zip \
    --region $LAMBDA_REGION \
    --output table \
    --query 'FunctionArn'