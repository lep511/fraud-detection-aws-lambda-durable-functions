"""
FastAPI entry point — Fraud Detection Service
=============================================
Exposes a /invocations endpoint that delegates all fraud analysis
to the Strands-powered agent defined in agent_fraud_detection.py.
"""

import logging
import os
import time
import traceback

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any

# ─────────────────────────────────────────────
# LOGGING SETUP — outputs to stdout for CloudWatch
# ─────────────────────────────────────────────
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
logging.basicConfig(
    level=LOG_LEVEL,
    format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
    force=True,
)
logger = logging.getLogger("fraud-agent.api")

logger.info("Starting Fraud Detection Agent — loading modules...")

# Import the fraud detection agent function from the sibling module
from agent_fraud_detection import analyze_transaction

logger.info("Module agent_fraud_detection loaded successfully")

app = FastAPI(title="Fraud Detection Agent", version="1.0.0")


# ─────────────────────────────────────────────
# REQUEST / RESPONSE SCHEMAS
# ─────────────────────────────────────────────

class InvocationRequest(BaseModel):
    input: Dict[str, Any]

class InvocationResponse(BaseModel):
    output: Dict[str, Any]


# ─────────────────────────────────────────────
# ENDPOINTS
# ─────────────────────────────────────────────

@app.post("/invocations", response_model=InvocationResponse)
async def invoke_agent(request: InvocationRequest):
    """
    Receive a transaction payload and return a fraud risk assessment.

    Expected input fields:
        - id       (int, optional) : Transaction identifier
        - amount   (float)         : Transaction amount in USD  ← required
        - location (str, optional) : City / region
        - vendor   (str, optional) : Merchant name

    Returns:
        output.risk_score  : int  — 1 (safe) to 5 (fraud)
        output.risk_detail : str  — explanation of the assessment
        output.amount      : float — original transaction amount
    """
    request_id = f"txn-{time.time_ns()}"
    logger.info(f"[{request_id}] POST /invocations — received request: {request.input}")

    try:
        input_data: Dict[str, Any] = request.input

        # Validate that 'amount' is present — it is the minimum required field
        if "amount" not in input_data or not input_data["amount"]:
            logger.warning(f"[{request_id}] Missing 'amount' field in request")
            raise HTTPException(
                status_code=400,
                detail="Amount not provided. Please include 'amount' (in USD) in the request."
            )

        # Delegate full analysis to the Strands fraud detection agent
        logger.info(f"[{request_id}] Calling analyze_transaction with: {input_data}")
        start = time.time()
        result: Dict[str, Any] = analyze_transaction(input_data)
        elapsed = time.time() - start
        logger.info(f"[{request_id}] Agent completed in {elapsed:.2f}s — result: {result}")

        return InvocationResponse(output=result)

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"[{request_id}] Agent processing FAILED: {type(e).__name__}: {e}")
        logger.error(f"[{request_id}] Full traceback:\n{traceback.format_exc()}")
        raise HTTPException(
            status_code=500,
            detail=f"Agent processing failed: {str(e)}"
        )


@app.get("/ping")
async def ping():
    """Health check endpoint."""
    return {
        "status": "Fraud Detection Agent is running and healthy.",
        "usage": "POST /invocations with body: {'input': {'id': 1, 'amount': 6500, 'location': 'Los Angeles', 'vendor': 'Electronics Store'}}"
    }


# ─────────────────────────────────────────────
# ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)