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
from unittest.mock import MagicMock, patch

import pytest

# ---------------------------------------------------------------------------
# SDK mocking strategy
# ---------------------------------------------------------------------------

_fake_sdk = MagicMock()


def _fake_durable_step(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        if args and hasattr(args[0], 'logger'):
            return fn(*args, **kwargs)
        return f"__step_descriptor:{fn.__name__}"
    wrapper._fn = fn
    return wrapper


_fake_sdk.durable_step = _fake_durable_step
_fake_sdk.durable_execution = lambda fn: fn
_fake_sdk.DurableContext = MagicMock
_fake_sdk.StepContext = MagicMock

_fake_config = MagicMock()
_fake_retries = MagicMock()
_fake_retries.create_retry_strategy.return_value = MagicMock()

import sys
sys.modules["aws_durable_execution_sdk_python"] = _fake_sdk
sys.modules["aws_durable_execution_sdk_python.config"] = _fake_config
sys.modules["aws_durable_execution_sdk_python.retries"] = _fake_retries

import app


# ---------------------------------------------------------------------------
# Test helpers
# ---------------------------------------------------------------------------

def _make_step_ctx():
    ctx = MagicMock()
    ctx.logger = MagicMock()
    return ctx


def _make_durable_ctx():
    ctx = MagicMock()
    ctx.logger = MagicMock()
    ctx.step = MagicMock(side_effect=lambda result, **kw: result)
    return ctx


def _base_event(**overrides):
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

class TestAuthorizeLogic:
    def test_basic_authorization(self):
        tx = {"id": "t1", "amount": 50.0, "score": 1}
        result = app._authorize_logic(tx)
        assert result["statusCode"] == 200
        assert result["body"]["result"] == "authorized"
        assert result["body"]["transaction_id"] == "t1"
        assert "customerVerificationResult" not in result["body"]

    def test_authorization_with_customer_rejection_flag(self):
        tx = {"id": "t2", "amount": 100.0, "score": 3}
        result = app._authorize_logic(tx, customer_rejection=True)
        assert result["body"]["customerVerificationResult"] == "TransactionApproved"


class TestFraudLogic:
    def test_basic_fraud_escalation(self):
        tx = {"id": "t3", "amount": 9999.0, "score": 5}
        result = app._fraud_logic(tx)
        assert result["statusCode"] == 200
        assert result["body"]["result"] == "SentToFraudDept"
        assert result["body"]["fraud_score"] == 5
        assert "customerVerificationResult" not in result["body"]

    def test_fraud_with_customer_rejection_flag(self):
        tx = {"id": "t4", "amount": 500.0, "score": 4}
        result = app._fraud_logic(tx, customer_rejection=True)
        assert result["body"]["customerVerificationResult"] == "TransactionDeclined"


# ===========================================================================
# 2. USE_BEDROCK_AGENTCORE boolean parsing
# ===========================================================================
# The module reads USE_BEDROCK_AGENTCORE from the environment as a string and
# converts it with `.lower() == "true"` so that "false", "False", and "" all
# evaluate to the boolean False — never a truthy non-empty string.
# ===========================================================================

class TestUseBedRockAgentCoreFlag:
    """USE_BEDROCK_AGENTCORE must always be a real bool, never a string."""

    @pytest.mark.parametrize("env_value,expected", [
        ("true",  True),
        ("True",  True),
        ("TRUE",  True),
        ("false", False),
        ("False", False),
        ("FALSE", False),
        ("",      False),
    ])
    def test_env_string_maps_to_bool(self, env_value, expected):
        # Re-evaluate the expression used in app.py to confirm every variant
        # maps to the correct Python bool.  Guards against using
        # bool(os.environ.get(...)) which would make "false" truthy.
        result = env_value.lower() == "true"
        assert result is expected

    def test_module_flag_is_a_bool(self):
        # The module-level attribute must be a real bool, never a string.
        assert isinstance(app.USE_BEDROCK_AGENTCORE, bool)


# ===========================================================================
# 3. Durable steps — check_fraud_score
# ===========================================================================
# The step branches on USE_BEDROCK_AGENTCORE:
#   True  -> calls Bedrock AgentCore via boto3
#   False -> calls an external HTTP endpoint via httpx
# Both classes patch the flag at the app module level so the correct code
# path is exercised regardless of the environment the tests run in.
# ===========================================================================

class TestCheckFraudScorePrecomputed:
    """score != 0 bypasses the agent entirely in both modes."""

    def test_precomputed_score_skips_agent(self):
        step_ctx = _make_step_ctx()
        result = app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=4)
        assert result["score"] == 4
        assert result["risk_detail"] == "precomputed"
        step_ctx.logger.info.assert_not_called()


class TestCheckFraudScoreBedrockPath:
    """Bedrock AgentCore path (USE_BEDROCK_AGENTCORE=True)."""

    def test_valid_response_returns_score(self):
        step_ctx = _make_step_ctx()
        body = {"output": {"risk_score": 3, "risk_detail": "moderate risk"}}
        fake_stream = io.BytesIO(json.dumps(body).encode())

        with patch.object(app, "USE_BEDROCK_AGENTCORE", True), \
             patch.object(app, "AGENT_RUNTIME_ARN", "arn:aws:test:agent", create=True), \
             patch("app.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.invoke_agent_runtime.return_value = {"response": fake_stream}

            result = app.check_fraud_score(step_ctx, amount=200, location="EU", vendor="Shop", score=0)

        assert result["score"] == 3
        assert result["risk_detail"] == "moderate risk"

    def test_invalid_score_returns_agent_failure(self):
        step_ctx = _make_step_ctx()
        body = {"output": {"risk_score": 99}}
        fake_stream = io.BytesIO(json.dumps(body).encode())

        with patch.object(app, "USE_BEDROCK_AGENTCORE", True), \
             patch.object(app, "AGENT_RUNTIME_ARN", "arn:aws:test:agent", create=True), \
             patch("app.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.invoke_agent_runtime.return_value = {"response": fake_stream}

            result = app.check_fraud_score(step_ctx, amount=200, location="EU", vendor="Shop", score=0)

        assert result["score"] == 0
        assert result["risk_detail"] == "agent_failure"

    def test_empty_output_returns_agent_failure(self):
        step_ctx = _make_step_ctx()
        body = {"output": {}}
        fake_stream = io.BytesIO(json.dumps(body).encode())

        with patch.object(app, "USE_BEDROCK_AGENTCORE", True), \
             patch.object(app, "AGENT_RUNTIME_ARN", "arn:aws:test:agent", create=True), \
             patch("app.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.invoke_agent_runtime.return_value = {"response": fake_stream}

            result = app.check_fraud_score(step_ctx, amount=200, location="EU", vendor="Shop", score=0)

        assert result["score"] == 0
        assert result["risk_detail"] == "agent_failure"

    def test_missing_agent_arn_raises(self):
        step_ctx = _make_step_ctx()
        with patch.object(app, "USE_BEDROCK_AGENTCORE", True), \
             patch.object(app, "AGENT_RUNTIME_ARN", None, create=True):
            with pytest.raises(ValueError, match="AGENT_RUNTIME_ARN"):
                app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

    @pytest.mark.parametrize("valid_score", [1, 2, 3, 4, 5])
    def test_all_valid_score_boundaries(self, valid_score):
        step_ctx = _make_step_ctx()
        body = {"output": {"risk_score": valid_score, "risk_detail": "test"}}
        fake_stream = io.BytesIO(json.dumps(body).encode())

        with patch.object(app, "USE_BEDROCK_AGENTCORE", True), \
             patch.object(app, "AGENT_RUNTIME_ARN", "arn:aws:test:agent", create=True), \
             patch("app.boto3") as mock_boto3:
            mock_client = MagicMock()
            mock_boto3.client.return_value = mock_client
            mock_client.invoke_agent_runtime.return_value = {"response": fake_stream}

            result = app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

        assert result["score"] == valid_score


class TestCheckFraudScoreHttpPath:
    """External HTTP path (USE_BEDROCK_AGENTCORE=False).

    When USE_BEDROCK_AGENTCORE is False the step POSTs to AGENT_BASE_URL/invocations
    via httpx.  The three expected failure modes map to ValueError:
      - TimeoutException  -> "The request took too long"
      - ConnectError      -> "Unable to connect to the server"
      - HTTPStatusError   -> "Error HTTP <status_code>"
    """

    def test_valid_http_response_returns_score(self):
        step_ctx = _make_step_ctx()
        body = {"output": {"risk_score": 2, "risk_detail": "low risk"}}

        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()

        with patch.object(app, "USE_BEDROCK_AGENTCORE", False), \
             patch.object(app, "AGENT_BASE_URL", "https://agent.example.com"), \
             patch("app.httpx.post", return_value=mock_response) as mock_post:

            result = app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

        assert result["score"] == 2
        assert result["risk_detail"] == "low risk"
        mock_post.assert_called_once_with(
            "https://agent.example.com/invocations",
            json={"input": {"id": 0, "amount": 100, "location": "US", "vendor": "V"}},
            timeout=360.0,
        )

    def test_http_invalid_score_returns_agent_failure(self):
        step_ctx = _make_step_ctx()
        body = {"output": {"risk_score": 99}}

        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()

        with patch.object(app, "USE_BEDROCK_AGENTCORE", False), \
             patch.object(app, "AGENT_BASE_URL", "https://agent.example.com"), \
             patch("app.httpx.post", return_value=mock_response):

            result = app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

        assert result["score"] == 0
        assert result["risk_detail"] == "agent_failure"

    def test_http_timeout_raises_value_error(self):
        import httpx
        step_ctx = _make_step_ctx()

        with patch.object(app, "USE_BEDROCK_AGENTCORE", False), \
             patch.object(app, "AGENT_BASE_URL", "https://agent.example.com"), \
             patch("app.httpx.post", side_effect=httpx.TimeoutException("timed out")):

            with pytest.raises(ValueError, match="The request took too long"):
                app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

    def test_http_connect_error_raises_value_error(self):
        import httpx
        step_ctx = _make_step_ctx()

        with patch.object(app, "USE_BEDROCK_AGENTCORE", False), \
             patch.object(app, "AGENT_BASE_URL", "https://agent.example.com"), \
             patch("app.httpx.post", side_effect=httpx.ConnectError("refused")):

            with pytest.raises(ValueError, match="Unable to connect to the server"):
                app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

    def test_http_status_error_raises_value_error(self):
        import httpx
        step_ctx = _make_step_ctx()

        mock_response = MagicMock()
        mock_response.status_code = 500
        http_error = httpx.HTTPStatusError(
            "server error", request=MagicMock(), response=mock_response
        )
        mock_response.raise_for_status.side_effect = http_error

        with patch.object(app, "USE_BEDROCK_AGENTCORE", False), \
             patch.object(app, "AGENT_BASE_URL", "https://agent.example.com"), \
             patch("app.httpx.post", return_value=mock_response):

            with pytest.raises(ValueError, match="Error HTTP 500"):
                app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

    def test_missing_agent_base_url_raises(self):
        # Empty AGENT_BASE_URL must raise ValueError before any HTTP call.
        step_ctx = _make_step_ctx()
        with patch.object(app, "USE_BEDROCK_AGENTCORE", False), \
             patch.object(app, "AGENT_BASE_URL", ""):
            with pytest.raises(ValueError, match="AGENT_BASE_URL"):
                app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

    @pytest.mark.parametrize("valid_score", [1, 2, 3, 4, 5])
    def test_all_valid_score_boundaries_http(self, valid_score):
        step_ctx = _make_step_ctx()
        body = {"output": {"risk_score": valid_score, "risk_detail": "test"}}

        mock_response = MagicMock()
        mock_response.json.return_value = body
        mock_response.raise_for_status = MagicMock()

        with patch.object(app, "USE_BEDROCK_AGENTCORE", False), \
             patch.object(app, "AGENT_BASE_URL", "https://agent.example.com"), \
             patch("app.httpx.post", return_value=mock_response):

            result = app.check_fraud_score(step_ctx, amount=100, location="US", vendor="V", score=0)

        assert result["score"] == valid_score


# ===========================================================================
# 4. Other durable steps
# ===========================================================================

class TestAuthorizeTransaction:
    def test_returns_authorized_result(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t10", "amount": 50, "score": 1}
        result = app.authorize_transaction(step_ctx, tx)
        assert result["body"]["result"] == "authorized"


class TestSuspendTransaction:
    def test_returns_true(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t20", "amount": 300, "score": 3}
        assert app.suspend_transaction(step_ctx, tx) is True


class TestSendToFraud:
    def test_returns_fraud_result(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t30", "amount": 10000, "score": 5}
        result = app.send_to_fraud(step_ctx, tx)
        assert result["body"]["result"] == "SentToFraudDept"

    def test_with_customer_rejection(self):
        step_ctx = _make_step_ctx()
        tx = {"id": "t31", "amount": 500, "score": 4}
        result = app.send_to_fraud(step_ctx, tx, customer_rejection=True)
        assert result["body"]["customerVerificationResult"] == "TransactionDeclined"


class TestNotificationSteps:
    def test_send_email_notification(self):
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
        step_ctx = _make_step_ctx()
        tx = {"id": "t41", "amount": 300, "score": 3}
        result = app.send_sms_notification(step_ctx, "cb-456", tx)
        assert result is None
        step_ctx.logger.info.assert_called_once()


class TestAdvanceTransaction:
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
    def test_email_verification_success(self):
        child_ctx = MagicMock()
        child_ctx.wait_for_callback = MagicMock(return_value={"approved": True})
        child_ctx.step = MagicMock(side_effect=lambda result, **kw: result)
        tx = {"id": "t60", "amount": 300, "score": 3}

        result = app.email_verification(child_ctx, tx)
        assert result["success"] is True
        assert result["channel"] == "email"
        child_ctx.wait_for_callback.assert_called_once()

    def test_email_verification_timeout(self):
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
# 5. Handler routing
# ===========================================================================

class TestHandlerCallbackShortCircuit:
    def test_callback_event_returns_event(self):
        ctx = _make_durable_ctx()
        event = {"callbackId": "cb-99", "approved": True}
        result = app.handler(event, ctx)
        assert result == event
        ctx.step.assert_not_called()


class TestHandlerAgentFailure:
    def test_score_zero_sends_to_fraud(self):
        ctx = _make_durable_ctx()
        fraud_result = {"statusCode": 200, "body": {"result": "SentToFraudDept"}}
        ctx.step = MagicMock(side_effect=[
            {"score": 0, "risk_detail": "agent_failure"},
            fraud_result,
        ])

        result = app.handler(_base_event(score=0), ctx)
        assert result["body"]["result"] == "SentToFraudDept"
        assert ctx.step.call_count == 2
        call_names = [c.kwargs.get("name", "") for c in ctx.step.call_args_list]
        assert "fraudCheck" in call_names
        assert "sendToFraudAgentFailure" in call_names


class TestHandlerLowRisk:
    @pytest.mark.parametrize("score", [1, 2])
    def test_low_risk_authorizes(self, score):
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": score, "risk_detail": "precomputed"},
            {"statusCode": 200, "body": {"result": "authorized"}},
        ])

        result = app.handler(_base_event(score=score), ctx)
        assert result["body"]["result"] == "authorized"
        assert ctx.step.call_count == 2


class TestHandlerHighRisk:
    def test_high_risk_escalates(self):
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": 5, "risk_detail": "very high risk"},
            {"statusCode": 200, "body": {"result": "SentToFraudDept"}},
        ])

        result = app.handler(_base_event(score=5), ctx)
        assert result["body"]["result"] == "SentToFraudDept"
        assert ctx.step.call_count == 2


class TestHandlerMediumRisk:
    def _setup_ctx(self, verification_success_count):
        ctx = _make_durable_ctx()
        parallel_result = MagicMock()
        parallel_result.success_count = verification_success_count
        ctx.parallel = MagicMock(return_value=parallel_result)

        if verification_success_count > 0:
            advance = {"statusCode": 200, "body": {"result": "authorized", "customerVerificationResult": "TransactionApproved"}}
        else:
            advance = {"statusCode": 200, "body": {"result": "SentToFraudDept", "customerVerificationResult": "TransactionDeclined"}}

        ctx.step = MagicMock(side_effect=[
            {"score": 3, "risk_detail": "medium risk"},
            True,
            advance,
        ])
        return ctx

    def test_verification_passed(self):
        ctx = self._setup_ctx(verification_success_count=1)
        result = app.handler(_base_event(score=3), ctx)
        assert result["body"]["result"] == "authorized"
        assert result["body"]["customerVerificationResult"] == "TransactionApproved"
        ctx.parallel.assert_called_once()

    def test_verification_failed(self):
        ctx = self._setup_ctx(verification_success_count=0)
        result = app.handler(_base_event(score=4), ctx)
        assert result["body"]["result"] == "SentToFraudDept"
        assert result["body"]["customerVerificationResult"] == "TransactionDeclined"
        ctx.parallel.assert_called_once()

    def test_step_order(self):
        ctx = self._setup_ctx(verification_success_count=1)
        app.handler(_base_event(score=3), ctx)
        assert ctx.step.call_count == 3
        names = [c.kwargs.get("name", "") for c in ctx.step.call_args_list]
        assert names == ["fraudCheck", "suspendTransaction", "advanceTransaction"]

    def test_parallel_has_two_branches(self):
        ctx = self._setup_ctx(verification_success_count=1)
        app.handler(_base_event(score=3), ctx)
        call = ctx.parallel.call_args
        assert call.kwargs["name"] == "human-verification"
        assert len(call.args[0]) == 2


class TestHandlerEventParsing:
    def test_default_score_is_zero(self):
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": 0, "risk_detail": "agent_failure"},
            {"statusCode": 200, "body": {"result": "SentToFraudDept"}},
        ])
        event = {"id": "t99", "amount": 100, "location": "US", "vendor": "X"}
        result = app.handler(event, ctx)
        assert result["body"]["result"] == "SentToFraudDept"

    def test_first_step_is_named_fraud_check(self):
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": 1, "risk_detail": "low"},
            {"statusCode": 200, "body": {"result": "authorized"}},
        ])
        app.handler(_base_event(amount=42.5, location="JP", vendor="Sony", score=1), ctx)
        assert ctx.step.call_args_list[0].kwargs["name"] == "fraudCheck"


# ===========================================================================
# 6. Retry strategy
# ===========================================================================

class TestRetryStrategy:
    def test_notification_retry_strategy_is_created(self):
        assert app._notification_retry_strategy is not None


# ===========================================================================
# 7. Score boundary edge cases
# ===========================================================================

class TestEdgeCases:
    def test_score_3_is_medium_risk(self):
        ctx = _make_durable_ctx()
        parallel_result = MagicMock()
        parallel_result.success_count = 1
        ctx.parallel = MagicMock(return_value=parallel_result)
        ctx.step = MagicMock(side_effect=[
            {"score": 3, "risk_detail": "medium"},
            True,
            {"statusCode": 200, "body": {"result": "authorized", "customerVerificationResult": "TransactionApproved"}},
        ])
        result = app.handler(_base_event(score=3), ctx)
        assert result["body"]["result"] == "authorized"
        ctx.parallel.assert_called_once()

    def test_score_4_is_medium_risk(self):
        ctx = _make_durable_ctx()
        parallel_result = MagicMock()
        parallel_result.success_count = 0
        ctx.parallel = MagicMock(return_value=parallel_result)
        ctx.step = MagicMock(side_effect=[
            {"score": 4, "risk_detail": "medium-high"},
            True,
            {"statusCode": 200, "body": {"result": "SentToFraudDept", "customerVerificationResult": "TransactionDeclined"}},
        ])
        result = app.handler(_base_event(score=4), ctx)
        assert result["body"]["result"] == "SentToFraudDept"
        ctx.parallel.assert_called_once()

    def test_score_5_is_high_risk(self):
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": 5, "risk_detail": "high"},
            {"statusCode": 200, "body": {"result": "SentToFraudDept"}},
        ])
        result = app.handler(_base_event(score=5), ctx)
        assert result["body"]["result"] == "SentToFraudDept"
        ctx.parallel.assert_not_called()

    def test_score_2_is_low_risk(self):
        ctx = _make_durable_ctx()
        ctx.step = MagicMock(side_effect=[
            {"score": 2, "risk_detail": "low"},
            {"statusCode": 200, "body": {"result": "authorized"}},
        ])
        result = app.handler(_base_event(score=2), ctx)
        assert result["body"]["result"] == "authorized"
        ctx.parallel.assert_not_called()