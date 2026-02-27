"""
FastAPI entry point — Fraud Detection Service
=============================================
Exposes a /invocations endpoint that delegates all fraud analysis
to the Strands-powered agent defined in agent_fraud_detection.py.
"""

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel
from typing import Dict, Any

# Import the fraud detection agent function from the sibling module
from agent_fraud_detection import analyze_transaction

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
    try:
        input_data: Dict[str, Any] = request.input

        # Validate that 'amount' is present — it is the minimum required field
        if "amount" not in input_data or not input_data["amount"]:
            raise HTTPException(
                status_code=400,
                detail="Amount not provided. Please include 'amount' (in USD) in the request."
            )

        # Delegate full analysis to the Strands fraud detection agent
        # analyze_transaction returns: {risk_score, risk_detail, amount}
        result: Dict[str, Any] = analyze_transaction(input_data)

        return InvocationResponse(output=result)

    except HTTPException:
        # Re-raise HTTP exceptions without wrapping them
        raise
    except Exception as e:
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