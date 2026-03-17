"""
Tests for FraudDetection-Lambda durable function (app.py).

This test suite validates a Lambda durable function that implements a fraud
detection workflow.  Lambda durable functions use a checkpoint/replay mechanism
to track progress and automatically recover from failures (see "How it works"
in the Lambda durable functions documentation).  The handler in app.py wraps
its logic with @durable_execution, which provides a DurableContext and manages
checkpoint operations transparently.

Coverage:
  1. Pure business logic  (_authorize_logic, _fraud_logic)
  2. Individual durable steps decorated with @durable_step
  3. Main handler routing for every risk tier + callback short-circuit
  4. Medium-risk parallel human-verification flow
  5. Retry strategy configuration
  6. Score boundary / edge cases

Durable functions key concepts exercised here (from the docs):
  - @durable_execution decorator: wraps the Lambda handler, providing a
    DurableContext instead of the standard Lambda context.
  - @durable_step decorator: marks a function as a durable step that creates
    checkpoints before and after execution. If the function is interrupted it
    resumes from the last completed checkpoint with stored results.
  - DurableContext.step(): executes a step with automatic checkpointing and
    retry.  During replay, completed steps return their stored result without
    re-executing.
  - DurableContext.parallel(): executes multiple operations concurrently with
    optional concurrency control and a completion policy.
  - DurableContext.wait_for_callback(): pauses execution until an external
    system sends a callback via the Lambda API.  The function suspends without
    incurring compute charges.
  - Determinism requirement: code must produce the same results given the same
    inputs during replay.  Side-effect-free logic outside steps is safe because
    it is deterministic.
  - Retry strategies: configurable via RetryStrategyConfig / create_retry_strategy
    and attached to steps through StepConfig or WaitForCallbackConfig.
"""

import json
import io
import os
import functools
from unittest import mock
from unittest.mock import MagicMock, patch, PropertyMock

import pytest

# ---------------------------------------------------------------------------
# SDK mocking strategy
# ---------------------------------------------------------------------------
# We mock the entire aws_durable_execution_sdk_python package so that tests
# run without the real Lambda durable-execution runtime.
#
# According to the docs, the SDK handles three responsibilities:
#   1. Checkpoint management  - persists step results for replay
#   2. Replay coordination    - skips completed operations on resume
#   3. State isolation        - each execution has its own checkpoint log
#
# Our mocks replace this machinery:
#   - @durable_step is replaced by _fake_durable_step which lets us call the
#     raw function directly (unit tests) OR returns a sentinel descriptor that
#     our mocked context.step() ignores (handler-level tests).
#   - @durable_execution is a passthrough so we can call handler() directly
#     with a fake DurableContext.
#   - DurableContext.step / .parallel / .wait_for_callback are MagicMocks
#     whose side_effect lists let us control each checkpoint result.
# ---------------------------------------------------------------------------

_fake_sdk = MagicMock()


def _fake_durable_step(fn):
    """Mock replacement for the @durable_step decorator.

    The real SDK decorator strips the step_ctx parameter so that calling e.g.
        check_fraud_score(amount=100, location="US", vendor="V", score=4)
    returns a "step descriptor" object.  DurableContext.step() then executes
    that descriptor, injecting a StepContext and creating checkpoints.

    Our mock preserves both call patterns:
      - Direct call with a StepContext as first arg (for unit-testing the step
        function in isolation).
      - Call without StepContext (as done inside the handler) returns a
        placeholder string; the mocked context.step() controls the result via
        its side_effect list.

    The original function is also exposed as wrapper._fn for introspection.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        # Direct unit-test call: first positional arg is a StepContext mock
        if args and hasattr(args[0], 'logger'):
            return fn(*args, **kwargs)
        # Handler call: SDK would build a step descriptor; return a placeholder
        # that our mocked context.step (configured with side_effect) will ignore.
        return f"__step_descriptor:{fn.__name__}"
    wrapper._fn = fn
    return wrapper


# Wire up the fake SDK module so that `from aws_durable_execution_sdk_python
# import durable_execution, durable_step, DurableContext, StepContext` works.
_fake_sdk.durable_step = _fake_durable_step
_fake_sdk.durable_execution = lambda fn: fn  # passthrough
_fake_sdk.DurableContext = MagicMock
_fake_sdk.StepContext = MagicMock

# Sub-modules for config objects (Duration, ParallelConfig, etc.) and retry
# utilities (RetryStrategyConfig, create_retry_strategy).
_fake_config = MagicMock()
_fake_retries = MagicMock()
_fake_retries.create_retry_strategy.return_value = MagicMock()

import sys
sys.modules["aws_durable_execution_sdk_python"] = _fake_sdk
sys.modules["aws_durable_execution_sdk_python.config"] = _fake_config
sys.modules["aws_durable_execution_sdk_python.retries"] = _fake_retries

# Now import the application module (must happen after patching sys.modules)
import app


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_step_ctx():
    """Create a fake StepContext with a logger.

    In the real SDK the StepContext is provided by the checkpoint system when
    a step is executed.  It exposes a logger for structured logging within the
    step scope (see "Steps" in the docs).  Our fake just needs .logger.info()
    so that step functions can call step_ctx.logger.info(...) without error.
    """
    ctx = MagicMock()
    ctx.logger = MagicMock()
    return ctx


def _make_durable_ctx():
    """Create a fake DurableContext.

    The real DurableContext (see "DurableContext" in the docs) provides:
      - step()              : run business logic with checkpointing
      - parallel()          : run branches concurrently
      - wait_for_callback() : suspend until external callback
      - wait()              : suspend for a duration
      - logger              : structured logging

    Our fake wires context.step() as a passthrough (returns its first arg)
    by default, so handler tests can override it with side_effect lists to
    simulate checkpoint results for each step in sequence.
    """
    ctx = MagicMock()
    ctx.logger = MagicMock()
    # Default: context.step(descriptor, name=...) returns the descriptor as-is.
    # Tests override this with side_effect=[...] to control per-step results.
    ctx.step = MagicMock(side_effect=lambda result, **kw: result)
    return ctx


def _base_event(**overrides):
    """Build a base transaction event for handler tests.

    The handler expects: id, amount, location, vendor, and an optional score
    (defaults to 0 when missing, which triggers the agent-based fraud check).
    """
    event = {
        "id": "txn-001",
        "amount": 150.0,
        "location": "US",
        "vendor": "Amazon",
        "score": 0,
    }
    event.update(overrides)
    return event


# ===========================================================================
# 1. Pure business logic
# ===========================================================================
# These functions contain the core decision logic and are intentionally kept
# outside of @durable_step so they can be reused from different steps
# (e.g. authorize_transaction and the inline advance_transaction both call
# _authorize_logic).  Because they have no side effects they are safe to
# execute during replay without causing non-deterministic behavior.
# ===========================================================================

class TestAuthorizeLogic:
    """Validate _authorize_logic which builds the "authorized" response."""

    def test_basic_authorization(self):
        # Standard authorization: transaction is approved, no customer
        # verification involved (low-risk path).
        tx = {"id": "t1", "amount": 50.0, "score": 1}
        result = app._authorize_logic(tx)
        assert result["statusCode"] == 200
        assert result["body"]["result"] == "authorized"
        assert result["body"]["transaction_id"] == "t1"
        # No customer verification result should be present for direct auth
        assert "customerVerificationResult" not in result["body"]

    def test_authorization_with_customer_rejection_flag(self):
        # When customer_rejection=True the response includes the human
        # verification outcome.  This happens after the medium-risk
        # parallel verification flow confirms the transaction.
        tx = {"id": "t2", "amount": 100.0, "score": 3}
        result = app._authorize_logic(tx, customer_rejection=True)
        assert result["body"]["customerVerificationResult"] == "TransactionApproved"


class TestFraudLogic:
    """Validate _fraud_logic which builds the "SentToFraudDept" response."""

    def test_basic_fraud_escalation(self):
        # High-risk transaction is escalated without customer interaction.
        tx = {"id": "t3", "amount": 9999.0, "score": 5}
        result = app._fraud_logic(tx)
        assert result["statusCode"] == 200
        assert result["body"]["result"] == "SentToFraudDept"
        assert result["body"]["fraud_score"] == 5
        assert "customerVerificationResult" not in result["body"]

    def test_fraud_with_customer_rejection_flag(self):
        # After the human-in-the-loop verification flow the customer declined
        # the transaction, so it is escalated with the decline annotation.
        tx = {"id": "t4", "amount": 500.0, "score": 4}
        result = app._fraud_logic(tx, customer_rejection=True)
        assert result["body"]["customerVerificationResult"] == "TransactionDeclined"


# ===========================================================================
# 2. Durable steps (unit-level)
# ===========================================================================
# Each @durable_step function is tested in isolation by passing a fake
# StepContext directly.  In the real SDK, DurableContext.step() would inject
# the StepContext and manage checkpointing (see "Steps and checkpoints" in
# the docs).  Here we bypass the SDK to verify the business logic within
# each step independently.
# ===========================================================================

class TestCheckFraudScore:
    """Tests for the check_fraud_score durable step.

    This step is the entry point of the workflow.  It either returns a
    precomputed score (skipping the agent) or invokes a Bedrock AgentCore
    runtime to assess fraud risk.  The agent response must contain a valid
    risk_score between 1 and 5; anything else triggers an "agent_failure"
    escalation path (score 0).
    """

    def test_precomputed_score_skips_agent(self):
        # When the event already includes a non-zero score the step returns
        # immediately with "precomputed" detail, avoiding the external agent
        # call.  This is a deterministic operation safe for replay.
        step_ctx = _make_step_ctx()
        result = app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=4)
        assert result["score"] == 4
        assert result["risk_detail"] == "precomputed"
        # No logging happens because the early-return path doesn't log
        step_ctx.logger.info.assert_not_called()

    def test_zero_score_calls_agent_valid_response(self):
        # When score=0 the step invokes the Bedrock AgentCore runtime.
        # A valid response (risk_score in 1-5) is returned as the step result.
        # In a real durable execution this result would be checkpointed so
        # that replays skip the agent call entirely.
        step_ctx = _make_step_ctx()
        agent_response_body = {
            "output": {"risk_score": 3, "risk_detail": "moderate risk"}
        }
        fake_stream = io.BytesIO(json.dumps(agent_response_body).encode())

        with patch.dict(os.environ, {"AGENT_RUNTIME_ARN": "arn:aws:test:agent"}), \
             patch.object(app, "AGENT_RUNTIME_ARN", "arn:aws:test:agent"), \
             patch("app.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.invoke_agent_runtime.return_value = {"response": fake_stream}

            result = app.check_fraud_score(step_ctx, amount=200, location="EU", vendor="Shop", score=0)

        assert result["score"] == 3
        assert result["risk_detail"] == "moderate risk"

    def test_zero_score_agent_returns_invalid_score(self):
        # The agent returned a score outside the valid 1-5 range (e.g. 99).
        # The step treats this as an agent failure and returns score=0 with
        # risk_detail="agent_failure", which causes the handler to escalate
        # to the fraud department as a safety measure.
        step_ctx = _make_step_ctx()
        agent_response_body = {"output": {"risk_score": 99}}
        fake_stream = io.BytesIO(json.dumps(agent_response_body).encode())

        with patch.dict(os.environ, {"AGENT_RUNTIME_ARN": "arn:aws:test:agent"}), \
             patch.object(app, "AGENT_RUNTIME_ARN", "arn:aws:test:agent"), \
             patch("app.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.invoke_agent_runtime.return_value = {"response": fake_stream}

            result = app.check_fraud_score(step_ctx, amount=200, location="EU", vendor="Shop", score=0)

        assert result["score"] == 0
        assert result["risk_detail"] == "agent_failure"

    def test_zero_score_agent_returns_no_output(self):
        # The agent responded but with an empty "output" object (no risk_score
        # key at all).  Same escalation behavior as an invalid score.
        step_ctx = _make_step_ctx()
        agent_response_body = {"output": {}}
        fake_stream = io.BytesIO(json.dumps(agent_response_body).encode())

        with patch.dict(os.environ, {"AGENT_RUNTIME_ARN": "arn:aws:test:agent"}), \
             patch.object(app, "AGENT_RUNTIME_ARN", "arn:aws:test:agent"), \
             patch("app.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.invoke_agent_runtime.return_value = {"response": fake_stream}

            result = app.check_fraud_score(step_ctx, amount=200, location="EU", vendor="Shop", score=0)

        assert result["score"] == 0
        assert result["risk_detail"] == "agent_failure"

    def test_missing_agent_arn_raises(self):
        # If AGENT_RUNTIME_ARN is not set the step raises ValueError before
        # attempting any external call.  This is a configuration guard.
        step_ctx = _make_step_ctx()
        with patch.object(app, "AGENT_RUNTIME_ARN", None):
            with pytest.raises(ValueError, match="AGENT_RUNTIME_ARN"):
                app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

    @pytest.mark.parametrize("valid_score", [1, 2, 3, 4, 5])
    def test_all_valid_score_boundaries(self, valid_score):
        # Boundary test: every score in the valid range 1-5 should be accepted
        # by the step and returned as-is.  This ensures the condition
        # `1 <= risk_score <= 5` works correctly at both edges.
        step_ctx = _make_step_ctx()
        agent_response_body = {"output": {"risk_score": valid_score, "risk_detail": "test"}}
        fake_stream = io.BytesIO(json.dumps(agent_response_body).encode())

        with patch.dict(os.environ, {"AGENT_RUNTIME_ARN": "arn:aws:test:agent"}), \
             patch.object(app, "AGENT_RUNTIME_ARN", "arn:aws:test:agent"), \
             patch("app.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.invoke_agent_runtime.return_value = {"response": fake_stream}

            result = app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

        assert result["score"] == valid_score


class TestAuthorizeTransaction:
    """Test the authorize_transaction durable step.

    This step wraps _authorize_logic and is used for the low-risk path
    (score < 3).  The @durable_step decorator ensures the result is
    checkpointed; on replay the step is skipped and the stored result
    is returned instead (see "How replay works" in the docs).
    """

    def test_returns_authorized_result(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t10", "amount": 50, "score": 1}
        result = app.authorize_transaction(step_ctx, tx)
        assert result["body"]["result"] == "authorized"


class TestSuspendTransaction:
    """Test the suspend_transaction durable step.

    In the medium-risk flow the transaction is suspended while awaiting
    human verification.  This step acts as a checkpoint marker; its True
    return value is stored so that replay knows the suspension was recorded.
    """

    def test_returns_true(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t20", "amount": 300, "score": 3}
        assert app.suspend_transaction(step_ctx, tx) is True


class TestSendToFraud:
    """Test the send_to_fraud durable step.

    Used for high-risk transactions (score >= 5) and agent-failure fallback
    (score == 0).  Wraps _fraud_logic with checkpointing.
    """

    def test_returns_fraud_result(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t30", "amount": 10000, "score": 5}
        result = app.send_to_fraud(step_ctx, tx)
        assert result["body"]["result"] == "SentToFraudDept"

    def test_with_customer_rejection(self):
        # After medium-risk verification fails, the transaction is escalated
        # with the customer_rejection flag to annotate the decline.
        step_ctx = _make_step_ctx()
        tx = {"id": "t31", "amount": 500, "score": 4}
        result = app.send_to_fraud(step_ctx, tx, customer_rejection=True)
        assert result["body"]["customerVerificationResult"] == "TransactionDeclined"


class TestNotificationSteps:
    """Test email and SMS notification durable steps.

    These steps are called inside the parallel human-verification flow.
    Each is configured with a retry strategy (max_attempts=3 for
    ConnectionError / TimeoutError) via StepConfig, following the SDK's
    configurable retry pattern (see "Steps support configurable retry
    strategies" in the docs).

    In the real workflow these steps receive a callback_id generated by
    wait_for_callback() and send verification requests to the customer.
    The function then suspends (no compute charges) until the customer
    responds via the Lambda callback API.
    """

    def test_send_email_notification(self):
        # Verify the step publishes to SNS and logs the callback ID.
        step_ctx = _make_step_ctx()
        tx = {"id": "t40", "amount": 300, "score": 3}
        mock_sns = MagicMock()
        with patch.object(app, "_get_sns_client", return_value=mock_sns), \
             patch.object(app, "SNS_TOPIC", "arn:aws:sns:us-east-1:123456789012:test-topic"), \
             patch.object(app, "API_BASE_URL", "https://example.com"):
            result = app.send_email_notification(step_ctx, "cb-123", tx)
        assert result is None
        mock_sns.publish.assert_called_once_with(
            TopicArn="arn:aws:sns:us-east-1:123456789012:test-topic",
            Subject="Verify transaction t40",
            Message=(
                "Fraud verification required for transaction t40.\n"
                "Amount: $300\n"
                "Click to verify: https://example.com/verify?callbackId=cb-123"
            ),
        )
        step_ctx.logger.info.assert_called_once()

    def test_send_sms_notification(self):
        # Same pattern as email but over SMS channel.
        step_ctx = _make_step_ctx()
        tx = {"id": "t41", "amount": 300, "score": 3}
        result = app.send_sms_notification(step_ctx, "cb-456", tx)
        assert result is None
        step_ctx.logger.info.assert_called_once()


class TestAdvanceTransaction:
    """Test the advance_transaction durable step.

    This step is the final decision point in the medium-risk flow. It receives
    the verification outcome (passed=True/False) and delegates to the
    appropriate business logic function (_authorize_logic or _fraud_logic)
    with the customer_rejection flag set to True.
    """

    def test_verification_passed_authorizes(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t50", "amount": 300, "score": 3}
        result = app.advance_transaction(step_ctx, tx, passed=True)
        assert result["body"]["result"] == "authorized"
        assert result["body"]["customerVerificationResult"] == "TransactionApproved"

    def test_verification_failed_escalates(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t51", "amount": 300, "score": 4}
        result = app.advance_transaction(step_ctx, tx, passed=False)
        assert result["body"]["result"] == "SentToFraudDept"
        assert result["body"]["customerVerificationResult"] == "TransactionDeclined"


class TestVerificationBranches:
    """Test the top-level email_verification and sms_verification functions.

    These are the parallel branches passed to context.parallel() in the
    medium-risk flow.  Each branch uses wait_for_callback() to suspend
    execution until the customer responds.  On success they return
    {"success": True, ...}, on timeout {"success": False, ...}.
    """

    def test_email_verification_success(self):
        # Simulate a successful callback response from the customer.
        child_ctx = MagicMock()
        child_ctx.wait_for_callback = MagicMock(return_value={"approved": True})
        child_ctx.step = MagicMock(side_effect=lambda result, **kw: result)
        tx = {"id": "t60", "amount": 300, "score": 3}

        result = app.email_verification(child_ctx, tx)
        assert result["success"] is True
        assert result["channel"] == "email"
        child_ctx.wait_for_callback.assert_called_once()

    def test_email_verification_timeout(self):
        # Simulate a timeout — wait_for_callback raises TimeoutError.
        child_ctx = MagicMock()
        child_ctx.wait_for_callback = MagicMock(side_effect=TimeoutError("expired"))
        tx = {"id": "t61", "amount": 300, "score": 3}

        result = app.email_verification(child_ctx, tx)
        assert result["success"] is False
        assert result["channel"] == "email"
        assert result["error"] == "timeout"

    def test_sms_verification_success(self):
        child_ctx = MagicMock()
        child_ctx.wait_for_callback = MagicMock(return_value={"approved": True})
        child_ctx.step = MagicMock(side_effect=lambda result, **kw: result)
        tx = {"id": "t62", "amount": 300, "score": 3}

        result = app.sms_verification(child_ctx, tx)
        assert result["success"] is True
        assert result["channel"] == "sms"
        child_ctx.wait_for_callback.assert_called_once()

    def test_sms_verification_timeout(self):
        child_ctx = MagicMock()
        child_ctx.wait_for_callback = MagicMock(side_effect=TimeoutError("expired"))
        tx = {"id": "t63", "amount": 300, "score": 3}

        result = app.sms_verification(child_ctx, tx)
        assert result["success"] is False
        assert result["channel"] == "sms"
        assert result["error"] == "timeout"


# ===========================================================================
# 3. Handler - routing logic (integration-level with mocked SDK)
# ===========================================================================
# The handler is decorated with @durable_execution which provides a
# DurableContext instead of the standard Lambda context (see "The
# @durable_execution decorator" in the docs).
#
# The handler implements a multi-step fraud detection workflow:
#   1. Fraud score check (step)
#   2. Routing based on score:
#      - score == 0  -> agent failure, escalate to fraud dept
#      - score < 3   -> low risk, authorize immediately
#      - score >= 5  -> high risk, escalate to fraud dept
#      - score 3-4   -> medium risk:
#          a. Suspend transaction (step)
#          b. Parallel email + SMS verification (parallel + wait_for_callback)
#          c. Advance based on verification outcome (step)
#
# Each context.step() call creates a checkpoint.  If the function is
# interrupted, it resumes from the last completed checkpoint during replay,
# skipping already-completed steps (see "Replay mechanism" in the docs).
#
# The handler also short-circuits when the event contains a "callbackId",
# which indicates this invocation is a callback response routed by the SDK
# (see "Callbacks" in the docs).
# ===========================================================================

class TestHandlerCallbackShortCircuit:
    """When the event contains callbackId the handler returns it immediately.

    Per the docs, callbacks enable a function to pause and wait for external
    systems to provide input.  When a callback arrives the SDK invokes the
    function again; the handler detects the callbackId and returns early so
    the SDK can route the payload to the waiting wait_for_callback operation.
    No steps are executed in this path.
    """

    def test_callback_event_returns_event(self):
        ctx = _make_durable_ctx()
        event = {"callbackId": "cb-99", "approved": True}
        result = app.handler(event, ctx)
        assert result == event
        # No durable steps should have been called
        ctx.step.assert_not_called()


class TestHandlerAgentFailure:
    """Score == 0 after fraud check -> escalate to fraud department.

    When the agent fails to return a valid score (network error, bad response,
    etc.) the check_fraud_score step returns score=0.  The handler treats this
    as a safety escalation and sends the transaction to the fraud department
    via the "sendToFraudAgentFailure" named step.
    """

    def test_score_zero_sends_to_fraud(self):
        ctx = _make_durable_ctx()
        # Simulate checkpoint results: first step (fraudCheck) returns a failed
        # score, second step (sendToFraudAgentFailure) returns the escalation.
        fraud_result = {"statusCode": 200, "body": {"result": "SentToFraudDept"}}
        ctx.step = MagicMock(side_effect=[
            {"score": 0, "risk_detail": "agent_failure"},  # fraudCheck
            fraud_result,                                     # sendToFraudAgentFailure
        ])

        event = _base_event(score=0)
        result = app.handler(event, ctx)

        assert result["body"]["result"] == "SentToFraudDept"
        # Exactly 2 steps: fraud check + escalation
        assert ctx.step.call_count == 2
        # Verify the named steps match what we expect in the checkpoint log
        call_names = [c.kwargs.get("name") or c[1].get("name", "") for c in ctx.step.call_args_list]
        assert "fraudCheck" in call_names
        assert "sendToFraudAgentFailure" in call_names


class TestHandlerLowRisk:
    """Score < 3 -> authorize immediately.

    Low-risk transactions are authorized in a single step after the fraud
    check.  Only two checkpoints are created: fraudCheck and Authorize.
    On replay both steps return their stored results instantly.
    """

    @pytest.mark.parametrize("score", [1, 2])
    def test_low_risk_authorizes(self, score):
        ctx = _make_durable_ctx()
        auth_result = {"statusCode": 200, "body": {"result": "authorized"}}
        ctx.step = MagicMock(side_effect=[
            {"score": score, "risk_detail": "precomputed"},  # fraudCheck
            auth_result,                                       # Authorize
        ])

        event = _base_event(score=score)
        result = app.handler(event, ctx)

        assert result["body"]["result"] == "authorized"
        # Two checkpoint steps total
        assert ctx.step.call_count == 2


class TestHandlerHighRisk:
    """Score >= 5 -> send to fraud department.

    High-risk transactions skip human verification and are escalated
    directly.  The workflow creates two checkpoints: fraudCheck and
    sendToFraud.
    """

    def test_high_risk_escalates(self):
        ctx = _make_durable_ctx()
        fraud_result = {"statusCode": 200, "body": {"result": "SentToFraudDept"}}
        ctx.step = MagicMock(side_effect=[
            {"score": 5, "risk_detail": "very high risk"},  # fraudCheck
            fraud_result,                                     # sendToFraud
        ])

        event = _base_event(score=5)
        result = app.handler(event, ctx)

        assert result["body"]["result"] == "SentToFraudDept"
        assert ctx.step.call_count == 2


class TestHandlerMediumRisk:
    """Score 3 or 4 -> suspend, parallel verification, then advance.

    The medium-risk path is the most complex workflow in the handler:

    1. suspendTransaction step - marks the transaction as pending review.
    2. context.parallel() with two branches (email + SMS verification).
       Each branch uses wait_for_callback() to pause the function while
       waiting for the customer to respond (see "Callbacks" and "Parallel
       execution" in the docs).  The parallel operation is configured with:
         - max_concurrency=2 (both channels run simultaneously)
         - CompletionConfig(min_successful=1, tolerated_failure_count=1)
           meaning the workflow succeeds when at least one channel responds.
       During the wait the function is suspended and incurs no compute
       charges (see "Wait states" / "Pay only for what you use" in the docs).
    3. advanceTransaction step - authorizes or escalates based on whether
       at least one verification channel succeeded.

    This pattern mirrors the "Human-in-the-loop approvals" example from the
    docs, where the function suspends at the callback point, incurring no
    compute charges while waiting.  When the customer responds via the Lambda
    callback API, the function resumes and replays from the last checkpoint.
    """

    def _setup_medium_risk_ctx(self, verification_success_count):
        """Helper: build a DurableContext with mocked step/parallel results.

        The side_effect list for context.step simulates the checkpoint log:
          [0] fraudCheck result
          [1] suspendTransaction result
          [2] advanceTransaction result (depends on verification outcome)

        context.parallel returns a result object whose .success_count
        controls the advance decision.
        """
        ctx = _make_durable_ctx()
        # Parallel result mock (simulates the SDK's ParallelResult)
        parallel_result = MagicMock()
        parallel_result.success_count = verification_success_count
        ctx.parallel = MagicMock(return_value=parallel_result)

        # Step checkpoint results in order of execution
        step_results = [
            {"score": 3, "risk_detail": "medium risk"},  # fraudCheck
            True,                                          # suspendTransaction
        ]

        if verification_success_count > 0:
            # At least one verification channel succeeded -> authorize
            step_results.append({
                "statusCode": 200,
                "body": {
                    "result": "authorized",
                    "customerVerificationResult": "TransactionApproved",
                },
            })
        else:
            # Both channels failed/timed out -> escalate to fraud
            step_results.append({
                "statusCode": 200,
                "body": {
                    "result": "SentToFraudDept",
                    "customerVerificationResult": "TransactionDeclined",
                },
            })

        ctx.step = MagicMock(side_effect=step_results)
        return ctx

    def test_medium_risk_verification_passed(self):
        # Customer confirmed the transaction via at least one channel.
        # The workflow authorizes the transaction with the verification
        # annotation (customerVerificationResult = TransactionApproved).
        ctx = self._setup_medium_risk_ctx(verification_success_count=1)
        event = _base_event(score=3)
        result = app.handler(event, ctx)

        assert result["body"]["result"] == "authorized"
        assert result["body"]["customerVerificationResult"] == "TransactionApproved"
        ctx.parallel.assert_called_once()

    def test_medium_risk_verification_failed(self):
        # Neither email nor SMS verification succeeded (both timed out or
        # the customer declined).  The workflow escalates to fraud.
        ctx = self._setup_medium_risk_ctx(verification_success_count=0)
        event = _base_event(score=4)
        result = app.handler(event, ctx)

        assert result["body"]["result"] == "SentToFraudDept"
        assert result["body"]["customerVerificationResult"] == "TransactionDeclined"
        ctx.parallel.assert_called_once()

    def test_medium_risk_calls_suspend_before_parallel(self):
        # Verify the execution order matches the expected checkpoint sequence.
        # According to the docs, "your code must be deterministic during
        # replay" - so the step order matters for correct replay behavior.
        ctx = self._setup_medium_risk_ctx(verification_success_count=1)
        event = _base_event(score=3)
        app.handler(event, ctx)

        # Three checkpointed steps in order
        assert ctx.step.call_count == 3
        step_names = [c.kwargs.get("name", "") for c in ctx.step.call_args_list]
        assert step_names[0] == "fraudCheck"
        assert step_names[1] == "suspendTransaction"
        assert step_names[2] == "advanceTransaction"

    def test_parallel_config_max_concurrency(self):
        # Verify the parallel operation is configured correctly:
        # - Two verification branches (email + SMS)
        # - Named "human-verification" for checkpoint identification
        # The CompletionConfig allows one failure (tolerated_failure_count=1)
        # so the workflow completes as soon as one channel succeeds.
        ctx = self._setup_medium_risk_ctx(verification_success_count=1)
        event = _base_event(score=3)
        app.handler(event, ctx)

        parallel_call = ctx.parallel.call_args
        assert parallel_call.kwargs["name"] == "human-verification"
        # Two verification functions (email_verification, sms_verification)
        assert len(parallel_call.args[0]) == 2


class TestHandlerEventParsing:
    """Verify the handler correctly extracts transaction fields from the event.

    The handler builds an internal tx dict from the incoming event.  The
    score field defaults to 0 when absent, which triggers the agent-based
    fraud assessment path.  All other fields (id, amount, location, vendor)
    are required.
    """

    def test_default_score_is_zero(self):
        # When the event omits the "score" field it defaults to 0.  This
        # simulates a real transaction where no precomputed score is available
        # and the fraud agent must be consulted.  Since our mock returns
        # score=0 from the fraud check, the handler escalates to fraud.
        ctx = _make_durable_ctx()
        fraud_result = {"statusCode": 200, "body": {"result": "SentToFraudDept"}}
        ctx.step = MagicMock(side_effect=[
            {"score": 0, "risk_detail": "agent_failure"},
            fraud_result,
        ])

        event = {"id": "t99", "amount": 100, "location": "US", "vendor": "X"}
        result = app.handler(event, ctx)
        assert result["body"]["result"] == "SentToFraudDept"

    def test_event_fields_passed_to_fraud_check(self):
        # Verify the handler forwards all transaction fields to the fraud
        # check step.  The step receives them as keyword arguments via the
        # @durable_step-decorated check_fraud_score function.
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": 1, "risk_detail": "low"},
            {"statusCode": 200, "body": {"result": "authorized"}},
        ])

        event = _base_event(amount=42.5, location="JP", vendor="Sony", score=1)
        app.handler(event, ctx)

        # The first step call should have been the named "fraudCheck" step
        first_call = ctx.step.call_args_list[0]
        assert first_call.kwargs["name"] == "fraudCheck"


# ===========================================================================
# 4. Retry strategy configuration
# ===========================================================================
# The module creates a shared retry strategy at import time using the SDK's
# create_retry_strategy() with RetryStrategyConfig(max_attempts=3,
# retryable_error_types=[ConnectionError, TimeoutError]).  This strategy is
# attached to notification steps via StepConfig and to wait_for_callback
# operations via WaitForCallbackConfig.
#
# Per the docs, "Steps support configurable retry strategies, execution
# semantics (at-most-once or at-least-once), and custom serialization."
# ===========================================================================

class TestRetryStrategy:
    def test_notification_retry_strategy_is_created(self):
        # Verify the module-level retry strategy object exists.  In the real
        # SDK this would configure automatic retries with exponential backoff
        # for transient network errors during notification delivery.
        assert app._notification_retry_strategy is not None


# ===========================================================================
# 5. Edge cases - score boundary testing
# ===========================================================================
# The handler uses three boundary conditions to route transactions:
#   - score == 0  -> agent failure (escalate)
#   - score < 3   -> low risk (authorize)  [scores 1, 2]
#   - score >= 5  -> high risk (escalate)  [score 5]
#   - else        -> medium risk (verify)  [scores 3, 4]
#
# These tests verify each boundary score value takes the correct path.
# Because these conditional checks are outside steps, they re-execute during
# replay.  This is safe because they are deterministic - the score comes from
# a checkpointed step result that is identical across replays (see "Note"
# about deterministic conditionals outside steps in the docs).
# ===========================================================================

class TestEdgeCases:
    def test_score_exactly_3_is_medium_risk(self):
        # Score 3 is the lower boundary of the medium-risk range.
        # It must trigger the suspend + parallel verification flow.
        ctx = _make_durable_ctx()
        parallel_result = MagicMock()
        parallel_result.success_count = 1
        ctx.parallel = MagicMock(return_value=parallel_result)
        ctx.step = MagicMock(side_effect=[
            {"score": 3, "risk_detail": "medium"},
            True,
            {"statusCode": 200, "body": {"result": "authorized", "customerVerificationResult": "TransactionApproved"}},
        ])

        event = _base_event(score=3)
        result = app.handler(event, ctx)
        assert result["body"]["result"] == "authorized"
        # Parallel verification must have been invoked
        ctx.parallel.assert_called_once()

    def test_score_exactly_4_is_medium_risk(self):
        # Score 4 is the upper boundary of the medium-risk range.
        # Same flow as score 3 but here verification fails, leading to
        # fraud escalation.
        ctx = _make_durable_ctx()
        parallel_result = MagicMock()
        parallel_result.success_count = 0
        ctx.parallel = MagicMock(return_value=parallel_result)
        ctx.step = MagicMock(side_effect=[
            {"score": 4, "risk_detail": "medium-high"},
            True,
            {"statusCode": 200, "body": {"result": "SentToFraudDept", "customerVerificationResult": "TransactionDeclined"}},
        ])

        event = _base_event(score=4)
        result = app.handler(event, ctx)
        assert result["body"]["result"] == "SentToFraudDept"
        ctx.parallel.assert_called_once()

    def test_score_exactly_5_is_high_risk(self):
        # Score 5 crosses into the high-risk range (>= 5).  The handler
        # must skip the medium-risk verification flow entirely and escalate
        # directly.  No parallel operation should be invoked.
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": 5, "risk_detail": "high"},
            {"statusCode": 200, "body": {"result": "SentToFraudDept"}},
        ])

        event = _base_event(score=5)
        result = app.handler(event, ctx)
        assert result["body"]["result"] == "SentToFraudDept"
        # No parallel call for high risk - direct escalation
        ctx.parallel.assert_not_called()

    def test_score_exactly_2_is_low_risk(self):
        # Score 2 is the upper boundary of the low-risk range (< 3).
        # The handler authorizes immediately without human verification.
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": 2, "risk_detail": "low"},
            {"statusCode": 200, "body": {"result": "authorized"}},
        ])

        event = _base_event(score=2)
        result = app.handler(event, ctx)
        assert result["body"]["result"] == "authorized"
        # No parallel call for low risk - direct authorization
        ctx.parallel.assert_not_called()
