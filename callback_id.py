import boto3
import json

# ── Input ────────────────────────────────────────────────────────────────────

callback_id = input("Enter callbackId: ").strip()

if not callback_id:
    print("Error: callbackId cannot be empty.")
    exit(1)

# ── Send callback ────────────────────────────────────────────────────────────
# Durable Functions require a qualified ARN (with :$LATEST or a version number).
# Using just the function name is not supported for durable invocations.

client = boto3.client("lambda", region_name="us-east-1")

response = client.invoke(
    FunctionName="arn:aws:lambda:us-east-1:154395736719:function:fn-Fraud-Detection:$LATEST",
    InvocationType="Event",
    Payload=json.dumps({
        "callbackId": callback_id,
        "result": "approved"
    })
)

status = response["StatusCode"]

if status == 202:
    print(f"Callback sent successfully (HTTP {status})")
else:
    print(f"Unexpected response status: {status}")