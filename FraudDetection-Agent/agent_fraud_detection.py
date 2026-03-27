"""
Fraud Detection Agent built with Strands Agents SDK
=====================================================
This agent analyzes transactions and determines whether they are
potentially fraudulent using rule-based tools and an AI model.

Installation:
    pip install strands-agents strands-agents-tools

Usage:
    python fraud_detection_agent.py
"""

import json
import logging
import time
import traceback
import os
from strands import Agent, tool
from strands.models import BedrockModel  # or use AnthropicModel
from strands.models.openai import OpenAIModel # for OpenAI compatible

logger = logging.getLogger("fraud-agent.detection")


MODEL_ID = "us.anthropic.claude-sonnet-4-6"


# ─────────────────────────────────────────────
# FRAUD DETECTION TOOLS
# Each @tool function is made available to the agent
# ─────────────────────────────────────────────

@tool
def check_transaction_amount(amount: float, threshold: float = 5000.0) -> dict:
    """
    Check if a transaction amount exceeds the high-risk threshold.

    Args:
        amount: Transaction amount in USD.
        threshold: Maximum amount considered normal (default: $5,000).

    Returns:
        dict with risk level and details.
    """
    is_high_risk = amount > threshold
    risk_score = min(int((amount / threshold) * 40), 50)  # Max 50 points

    return {
        "check": "amount_check",
        "amount": amount,
        "threshold": threshold,
        "is_high_risk": is_high_risk,
        "risk_score": risk_score,
        "message": (
            f"Amount ${amount:,.2f} EXCEEDS threshold of ${threshold:,.2f} — HIGH RISK"
            if is_high_risk
            else f"Amount ${amount:,.2f} is within normal range"
        ),
    }


@tool
def check_vendor_risk(vendor: str) -> dict:
    """
    Evaluate the risk level of a vendor based on known fraud patterns.

    High-risk vendors are those commonly associated with fraud:
    electronics, gift cards, luxury items, wire transfers, etc.

    Args:
        vendor: Name of the merchant/vendor.

    Returns:
        dict with vendor risk classification.
    """
    # High-risk vendor keywords commonly found in fraudulent transactions
    high_risk_keywords = [
        "electronics", "gift card", "wire transfer", "crypto",
        "jewelry", "luxury", "gold", "forex", "bitcoin",
    ]
    # Medium-risk vendor keywords
    medium_risk_keywords = [
        "online", "gaming", "casino", "travel", "hotel",
        "airline", "international",
    ]

    vendor_lower = vendor.lower()

    if any(keyword in vendor_lower for keyword in high_risk_keywords):
        risk_level = "HIGH"
        risk_score = 30
    elif any(keyword in vendor_lower for keyword in medium_risk_keywords):
        risk_level = "MEDIUM"
        risk_score = 15
    else:
        risk_level = "LOW"
        risk_score = 5

    return {
        "check": "vendor_check",
        "vendor": vendor,
        "risk_level": risk_level,
        "risk_score": risk_score,
        "message": f"Vendor '{vendor}' classified as {risk_level} risk",
    }


@tool
def check_location_risk(location: str) -> dict:
    """
    Assess the fraud risk associated with a transaction location.

    Certain cities or regions have historically higher rates of
    card-not-present fraud or identity theft.

    Args:
        location: City or region of the transaction.

    Returns:
        dict with location risk assessment.
    """
    # Cities with statistically higher fraud rates in card transactions
    high_risk_locations = [
        "miami", "los angeles", "new york", "las vegas",
        "houston", "chicago", "atlanta",
    ]
    # International or less common locations flagged for manual review
    medium_risk_locations = [
        "dallas", "phoenix", "san francisco", "seattle",
    ]

    location_lower = location.lower()

    if any(city in location_lower for city in high_risk_locations):
        risk_level = "HIGH"
        risk_score = 20
    elif any(city in location_lower for city in medium_risk_locations):
        risk_level = "MEDIUM"
        risk_score = 10
    else:
        risk_level = "LOW"
        risk_score = 5

    return {
        "check": "location_check",
        "location": location,
        "risk_level": risk_level,
        "risk_score": risk_score,
        "message": f"Location '{location}' classified as {risk_level} risk",
    }


@tool
def calculate_fraud_score(amount_score: int, vendor_score: int, location_score: int) -> dict:
    """
    Aggregate individual risk scores into a final fraud verdict.

    Scoring thresholds:
        0–39   → LEGITIMATE
        40–64  → SUSPICIOUS (manual review recommended)
        65–100 → FRAUDULENT (block transaction)

    Args:
        amount_score: Risk score from amount check (0–50).
        vendor_score: Risk score from vendor check (0–30).
        location_score: Risk score from location check (0–20).

    Returns:
        dict with total score and fraud verdict.
    """
    total_score = amount_score + vendor_score + location_score
    total_score = min(total_score, 100)  # Cap at 100

    if total_score >= 65:
        verdict = "🚨 FRAUDULENT"
        action = "BLOCK transaction immediately and alert the cardholder"
        is_fraud = True
    elif total_score >= 40:
        verdict = "⚠️  SUSPICIOUS"
        action = "Flag for manual review — request additional verification"
        is_fraud = False
    else:
        verdict = "✅ LEGITIMATE"
        action = "Approve transaction"
        is_fraud = False

    return {
        "check": "fraud_score",
        "total_score": total_score,
        "verdict": verdict,
        "is_fraud": is_fraud,
        "recommended_action": action,
        "breakdown": {
            "amount_contribution": amount_score,
            "vendor_contribution": vendor_score,
            "location_contribution": location_score,
        },
    }


# ─────────────────────────────────────────────
# AGENT SYSTEM PROMPT
# Defines the agent's role, capabilities and behavior
# ─────────────────────────────────────────────

SYSTEM_PROMPT = """
You are FraudGuard, a fraud detection agent for a financial institution.
Your sole task is to evaluate whether a transaction is fraudulent using the tools provided.

## REQUIRED TOOL EXECUTION — Follow this exact sequence, no exceptions:

Step 1: call `check_transaction_amount(amount)`
Step 2: call `check_vendor_risk(vendor_name)`
Step 3: call `check_location_risk(location)`
Step 4: call `calculate_fraud_score(amount_score, vendor_score, location_score)`
         → use the three scores returned in steps 1–3

Do NOT skip, reorder, or infer any step. All four tools must be called for every transaction.

## OUTPUT RULES

After completing all tool calls, respond with a single raw JSON object.

Constraints:
- No markdown
- No code fences
- No explanation outside the JSON
- No additional keys

Required format:
{
    "risk_score": <integer 1–5>,
    "risk_detail": "<one concise sentence explaining the risk assessment>",
    "amount": <transaction amount as a number>
}

## RISK SCORE MAPPING

Map the total fraud score (0–100) returned by `calculate_fraud_score` to risk_score:

| Total Score | risk_score | Label          |
|-------------|------------|----------------|
| 0–19        | 1          | Safe           |
| 20–39       | 2          | Low risk       |
| 40–54       | 3          | Suspicious     |
| 55–69       | 4          | High risk      |
| 70–100      | 5          | Fraudulent     |

## ERROR HANDLING

If any tool call fails or returns an unexpected value:
- Do not guess or proceed
- Return: {"error": "Tool <tool_name> failed. Cannot assess transaction."}
"""


# ─────────────────────────────────────────────
# AGENT FACTORY
# Creates and returns a configured fraud detection agent
# ─────────────────────────────────────────────

def create_fraud_agent() -> Agent:
    """
    Instantiate the Strands fraud detection agent with all tools attached.

    Returns:
        Configured Agent ready to analyze transactions.
    """
    logger.info("Creating Strands agent...")
    model_api_key = os.environ.get("MODEL_API_KEY")

    # Check if model_api_key exist to use compatbile OpenAI models
    if model_api_key:
        validate_config(["MODEL_BASE_URL", "MODEL_NAME"])
        base_url = os.environ.get("MODEL_BASE_URL")
        model_name = os.environ.get("MODEL_NAME")
        
        try:
            model = OpenAIModel(
                client_args={
                    "api_key": model_api_key,
                    "base_url": "https://integrate.api.nvidia.com/v1"
                },
                model_id="z-ai/glm5",
                temperature=1,
                top_p=1,
                max_tokens=16384,
            )
        except Exception as e:
            logger.error(f"Failed to initialize OpenAI compatible agent: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            raise
    # Use Bedrock model with AWS identification
    else:
        try:
            model = BedrockModel(
                model_id=MODEL_ID,
                region_name="us-east-1",
            )
            logger.info("BedrockModel initialized successfully")
        except Exception as e:
            logger.error(f"Failed to initialize BedrockModel: {type(e).__name__}: {e}")
            logger.error(traceback.format_exc())
            raise

    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            check_transaction_amount,
            check_vendor_risk,
            check_location_risk,
            calculate_fraud_score,
        ],
        callback_handler=None,
    )

    logger.info("Strands Agent created successfully with 4 tools")
    return agent


# ─────────────────────────────────────────────
# ANALYSIS FUNCTION
# Formats the transaction and sends it to the agent
# ─────────────────────────────────────────────

def analyze_transaction(transaction: dict) -> dict:
    """
    Run a fraud analysis on a single transaction dictionary.

    Args:
        transaction: Dict containing at minimum:
            - id       (int)   : Transaction identifier
            - amount   (float) : Transaction amount in USD
            - location (str)   : City / region of transaction
            - vendor   (str)   : Merchant name

    Returns:
        dict with keys: risk_score (1-5), risk_detail (str), amount (float)
    """
    logger.info(f"analyze_transaction called with: {json.dumps(transaction)}")

    agent = create_fraud_agent()

    prompt = f"""
    Please analyze the following transaction for fraud:

    Transaction ID : {transaction.get('id')}
    Amount         : ${transaction.get('amount'):,}
    Location       : {transaction.get('location')}
    Vendor         : {transaction.get('vendor')}

    Use all available tools to perform a complete risk assessment and return
    ONLY the JSON response as instructed.
    """

    logger.debug(f"Sending prompt to agent:\n{prompt}")

    try:
        start = time.time()
        raw_response = str(agent(prompt)).strip()
        elapsed = time.time() - start
        logger.info(f"Agent raw response ({elapsed:.2f}s): {raw_response[:500]}")
    except Exception as e:
        logger.error(f"Agent invocation FAILED: {type(e).__name__}: {e}")
        logger.error(traceback.format_exc())
        raise

    try:
        response = json.loads(raw_response)
        logger.info(f"Parsed response successfully: {response}")
    except json.JSONDecodeError:
        logger.warning(f"JSON parse failed, attempting regex extraction from: {raw_response[:300]}")
        import re
        match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        if match:
            response = json.loads(match.group())
            logger.info(f"Regex extraction succeeded: {response}")
        else:
            logger.error(f"Could not extract JSON from agent response: {raw_response}")
            response = {"raw": raw_response}

    return response

def validate_config(required_vars: list):
    missing_vars = [var for var in required_vars if var not in os.environ]
    
    if missing_vars:
        raise EnvironmentError(
            f"The following environment variables are missing: {', '.join(missing_vars)}. "
            "Please set them before starting the application."
        )

# ─────────────────────────────────────────────
# MAIN — Demo with sample transactions
# ─────────────────────────────────────────────

if __name__ == "__main__":

    # Sample transactions to test the agent
    test_transactions = [
        # High-risk: Large amount + high-risk vendor + high-risk location
        {"id": 3, "amount": 6500, "location": "Los Angeles", "vendor": "Electronics Store"},

        # Low-risk: Small amount + low-risk vendor + low-risk location
        {"id": 7, "amount": 45, "location": "Portland", "vendor": "Coffee Shop"},

        # Medium-risk: Moderate amount + medium-risk vendor
        {"id": 12, "amount": 1200, "location": "Seattle", "vendor": "Online Gaming Store"},
    ]

    print("=" * 70)
    print("          🔍  FRAUD DETECTION AGENT — Powered by Strands")
    print("=" * 70)

    for tx in test_transactions:
        print(f"\n📋 Analyzing Transaction #{tx['id']} ...")
        print(f"   Raw data: {json.dumps(tx)}")
        print("-" * 70)

        result = analyze_transaction(tx)
        print(json.dumps(result, indent=2))
        print("=" * 70)