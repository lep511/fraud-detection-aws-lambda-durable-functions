import json
import os
from aws_durable_execution_sdk_python import (
    DurableContext,
    StepContext,
    durable_execution,
    durable_step,
)
from aws_durable_execution_sdk_python.config import (
    Duration,
    ParallelConfig,
    CompletionConfig,
    StepConfig,
    WaitForCallbackConfig,
)
from aws_durable_execution_sdk_python.retries import (
    RetryStrategyConfig,
    create_retry_strategy,
)
import boto3

AGENT_RUNTIME_ARN = os.environ.get("AGENT_RUNTIME_ARN")
AGENT_REGION = os.environ.get("AGENT_REGION", "us-east-1")
SNS_TOPIC = os.environ.get("SNS_TOPIC")
API_BASE_URL = os.environ.get("API_BASE_URL", "")

_sns_client = None


def _get_sns_client():
    global _sns_client
    if _sns_client is None:
        _sns_client = boto3.client("sns")
    return _sns_client


# ── Retry strategy ───────────────────────────────────────────────────────────

_notification_retry_strategy = create_retry_strategy(
    RetryStrategyConfig(
        max_attempts=3,
        retryable_error_types=[ConnectionError, TimeoutError],
    )
)


# ── Pure business logic ──────────────────────────────────────────────────────

def _authorize_logic(tx: dict, customer_rejection: bool = False) -> dict:
    result = {
        "statusCode": 200,
        "body": {
            "transaction_id": tx["id"],
            "amount": tx["amount"],
            "fraud_score": tx["score"],
            "result": "authorized",
        },
    }
    if customer_rejection:
        result["body"]["customerVerificationResult"] = "TransactionApproved"
    return result


def _fraud_logic(tx: dict, customer_rejection: bool = False) -> dict:
    result = {
        "statusCode": 200,
        "body": {
            "transaction_id": tx["id"],
            "amount": tx["amount"],
            "fraud_score": tx["score"],
            "result": "SentToFraudDept",
        },
    }
    if customer_rejection:
        result["body"]["customerVerificationResult"] = "TransactionDeclined"
    return result


# ── Durable steps ────────────────────────────────────────────────────────────

@durable_step
def check_fraud_score(
    step_ctx: StepContext,
    amount: float,
    location: str,
    vendor: str,
    score: int,
) -> dict:
    if score != 0:
        return {"score": score, "risk_detail": "precomputed"}

    step_ctx.logger.info("No score submitted, sending to Fraud Agent for assessment")

    if not AGENT_RUNTIME_ARN:
        raise ValueError("AGENT_RUNTIME_ARN environment variable is not set")

    client = boto3.client("bedrock-agentcore", region_name=AGENT_REGION)
    payload = json.dumps(
        {"input": {"amount": amount, "location": location, "vendor": vendor}}
    ).encode("utf-8")

    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=payload,
        contentType="application/json",
        accept="application/json",
    )

    response_body = json.loads(response["response"].read())
    step_ctx.logger.info(f"Agent response: {response_body}")

    risk_score = response_body.get("output", {}).get("risk_score")
    if risk_score is not None and 1 <= risk_score <= 5:
        return {
            "score": risk_score,
            "risk_detail": response_body["output"].get("risk_detail", "none"),
        }

    error_message = "No valid response from agent, sending to Ops department"
    step_ctx.logger.info(error_message)
    return {"score": 0, "risk_detail": "agent_failure", "error": error_message}


@durable_step
def authorize_transaction(step_ctx: StepContext, tx: dict, customer_rejection: bool = False) -> dict:
    step_ctx.logger.info(f"Authorizing transactionId: {tx['id']}")
    return _authorize_logic(tx, customer_rejection)


@durable_step
def suspend_transaction(step_ctx: StepContext, tx: dict) -> bool:
    step_ctx.logger.info(f"Suspending transactionId: {tx['id']}")
    return True


@durable_step
def send_to_fraud(step_ctx: StepContext, tx: dict, customer_rejection: bool = False) -> dict:
    step_ctx.logger.info(f"Escalating to fraud department - transactionId: {tx['id']}")
    return _fraud_logic(tx, customer_rejection)


@durable_step
def send_email_notification(step_ctx: StepContext, callback_id: str, tx: dict) -> None:
    verification_link = f"{API_BASE_URL}/verify?callbackId={callback_id}"
    message = (
        f"Fraud verification required for transaction {tx['id']}.\n"
        f"Amount: ${tx['amount']}\n"
        f"Click to verify: {verification_link}"
    )
    _get_sns_client().publish(
        TopicArn=SNS_TOPIC,
        Subject=f"Verify transaction {tx['id']}",
        Message=message,
    )
    step_ctx.logger.info(f"Email notification sent to SNS with callbackId: {callback_id}")


@durable_step
def send_sms_notification(step_ctx: StepContext, callback_id: str, tx: dict) -> None:
    step_ctx.logger.info(f"SMS notification sent with callbackId: {callback_id}")


@durable_step
def advance_transaction(step_ctx: StepContext, tx: dict, passed: bool) -> dict:
    if passed:
        step_ctx.logger.info("Verification passed — authorizing transaction")
        return _authorize_logic(tx, customer_rejection=True)
    step_ctx.logger.info("Verification failed — escalating to fraud department")
    return _fraud_logic(tx, customer_rejection=True)


# ── Parallel verification branches ───────────────────────────────────────────

def email_verification(child_ctx: DurableContext, tx: dict) -> dict:
    try:
        result = child_ctx.wait_for_callback(
            lambda callback_id, _: child_ctx.step(
                send_email_notification(callback_id, tx),
                name="SendVerificationEmail",
                config=StepConfig(retry_strategy=_notification_retry_strategy),
            ),
            name="emailVerification",
            config=WaitForCallbackConfig(
                timeout=Duration.from_days(1),
                retry_strategy=_notification_retry_strategy,
            ),
        )
        return {"success": True, "channel": "email", "result": result}
    except TimeoutError:
        return {"success": False, "channel": "email", "error": "timeout"}


def sms_verification(child_ctx: DurableContext, tx: dict) -> dict:
    try:
        result = child_ctx.wait_for_callback(
            lambda callback_id, _: child_ctx.step(
                send_sms_notification(callback_id, tx),
                name="SendVerificationSMS",
                config=StepConfig(retry_strategy=_notification_retry_strategy),
            ),
            name="smsVerification",
            config=WaitForCallbackConfig(
                timeout=Duration.from_days(1),
                retry_strategy=_notification_retry_strategy,
            ),
        )
        return {"success": True, "channel": "sms", "result": result}
    except TimeoutError:
        return {"success": False, "channel": "sms", "error": "timeout"}


# ── Main handler ─────────────────────────────────────────────────────────────

@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    # Callback response — return immediately so the SDK routes it
    if "callbackId" in event:
        context.logger.info(f"Callback received for callbackId: {event['callbackId']}")
        return event

    tx = {
        "id": event["id"],
        "amount": event["amount"],
        "location": event["location"],
        "vendor": event["vendor"],
        "score": event.get("score", 0),
    }

    # Step 1: Get or compute the fraud risk score
    fraud_result = context.step(
        check_fraud_score(
            amount=tx["amount"],
            location=tx["location"],
            vendor=tx["vendor"],
            score=tx["score"],
        ),
        name="fraudCheck",
    )
    tx["score"] = fraud_result["score"]
    tx["risk_detail"] = fraud_result["risk_detail"]
    context.logger.info(f"Transaction Score = {tx['score']}")

    # Score 0 — agent failure, escalate as safety measure
    if tx["score"] == 0:
        context.logger.info("Agent returned score 0 (failure) — escalating to fraud department")
        return context.step(send_to_fraud(tx), name="sendToFraudAgentFailure")

    # Low risk (1-2) — authorize immediately
    if tx["score"] < 3:
        return context.step(authorize_transaction(tx), name="Authorize")

    # High risk (5) — escalate to fraud department
    if tx["score"] >= 5:
        return context.step(send_to_fraud(tx), name="sendToFraud")

    # Medium risk (3-4) — suspend and request human verification

    # Step 2: Suspend the transaction
    context.step(suspend_transaction(tx), name="suspendTransaction")

    # Step 3: Parallel email + SMS verification
    verified = context.parallel(
        [
            lambda child_ctx: email_verification(child_ctx, tx),
            lambda child_ctx: sms_verification(child_ctx, tx),
        ],
        name="human-verification",
        config=ParallelConfig(
            max_concurrency=2,
            completion_config=CompletionConfig(
                min_successful=1,
                tolerated_failure_count=1,
            ),
        ),
    )

    # Step 4: Advance based on verification outcome
    return context.step(
        advance_transaction(tx, verified.success_count > 0),
        name="advanceTransaction",
    )
