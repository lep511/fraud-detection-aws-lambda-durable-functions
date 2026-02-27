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


# ── Retry strategy ───────────────────────────────────────────────────────────
# Shared retry config used for notification steps (email and SMS).

_notification_retry_strategy = create_retry_strategy(
    RetryStrategyConfig(
        max_attempts=3,
        retryable_error_types=[ConnectionError, TimeoutError],
    )
)


# ── Pure business logic functions ────────────────────────────────────────────
# These functions hold the core logic and can be called from any context,
# including inside other @durable_step definitions.

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
    score: int
) -> int:
    # If a score was already provided, skip the agent call
    if score != 0:
        return score

    step_ctx.logger.info("No score submitted, sending to Fraud Agent for assessment")

    if not AGENT_RUNTIME_ARN:
        raise ValueError("AGENT_RUNTIME_ARN environment variable is not set")

    client = boto3.client("bedrock-agentcore", region_name=AGENT_REGION)
    payload = json.dumps({"input": {"amount": amount, "location": location, "vendor": vendor}}).encode("utf-8")

    response = client.invoke_agent_runtime(
        agentRuntimeArn=AGENT_RUNTIME_ARN,
        qualifier="DEFAULT",
        payload=payload,
        contentType="application/json",
        accept="application/json",
    )

    response_body = json.loads(response["response"].read())
    step_ctx.logger.info(f"Agent response: {response_body}")

    if response_body.get("output", {}).get("risk_score") is not None:
        return response_body["output"]["risk_score"]

    # No valid score returned — escalate to fraud department
    step_ctx.logger.info("No valid response from agent, sending to Fraud department")
    return 5


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
    step_ctx.logger.info(f"Email notification sent with callbackId: {callback_id}")


@durable_step
def send_sms_notification(step_ctx: StepContext, callback_id: str, tx: dict) -> None:
    step_ctx.logger.info(f"SMS notification sent with callbackId: {callback_id}")


# ── Main handler ─────────────────────────────────────────────────────────────

@durable_execution
def handler(event: dict, context: DurableContext) -> dict:
    # If the incoming event is a callback response, return it immediately.
    # The SDK will route it internally to the waiting wait_for_callback operation.
    # Without this check the handler would crash trying to read transaction fields
    # that don't exist in a callback payload.
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
    tx["score"] = context.step(
        check_fraud_score(
            amount=tx["amount"],
            location=tx["location"],
            vendor=tx["vendor"],
            score=tx["score"],
        ),
        name="fraudCheck",
    )

    context.logger.info(f"Transaction Score = {tx['score']}")

    # Low risk — authorize immediately
    if tx["score"] < 3:
        return context.step(authorize_transaction(tx), name="Authorize")

    # High risk — escalate to fraud department
    if tx["score"] >= 5:
        return context.step(send_to_fraud(tx), name="sendToFraud")

    # Medium risk — suspend and request human verification
    if 2 < tx["score"] < 5:

        # Step 2: Suspend the transaction while awaiting verification
        context.step(suspend_transaction(tx), name="suspendTransaction")

        # Step 3: Send email and SMS verification in parallel.
        # WaitForCallbackConfig accepts timeout as Duration object, not timeout_seconds.
        # retry_strategy is valid here since WaitForCallbackConfig extends CallbackConfig.
        def email_verification(child_ctx: DurableContext):
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
            except Exception as e:
                if "timeout" in str(e).lower():
                    context.logger.info(f"Email verification timed out: {e}")
                    return {"success": False, "channel": "email", "error": "timeout"}
                raise

        def sms_verification(child_ctx: DurableContext):
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
            except Exception as e:
                if "timeout" in str(e).lower():
                    context.logger.info(f"SMS verification timed out: {e}")
                    return {"success": False, "channel": "sms", "error": "timeout"}
                raise

        # Run both channels concurrently — succeed when at least one completes
        verified = context.parallel(
            [email_verification, sms_verification],
            name="human-verification",
            config=ParallelConfig(
                max_concurrency=2,
                completion_config=CompletionConfig(
                    min_successful=1,
                    tolerated_failure_count=1,
                ),
            ),
        )

        # Step 4: Advance the transaction based on verification outcome.
        # Uses pure logic functions to avoid any dependency on decorator internals.
        @durable_step
        def advance_transaction(step_ctx: StepContext) -> dict:
            has_success = verified.success_count > 0
            if has_success:
                step_ctx.logger.info("Verification passed — authorizing transaction")
                return _authorize_logic(tx, customer_rejection=True)
            step_ctx.logger.info("Verification failed — escalating to fraud department")
            return _fraud_logic(tx, customer_rejection=True)

        return context.step(advance_transaction(), name="advanceTransaction")

    return {
        "statusCode": 400,
        "body": {
            "transaction_id": tx["id"],
            "amount": tx["amount"],
            "fraud_score": tx["score"],
            "result": "Unknown",
        },
    }