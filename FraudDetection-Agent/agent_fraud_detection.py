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
from strands import Agent, tool
from strands.models import BedrockModel  # or use AnthropicModel


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
You are FraudGuard, an expert AI fraud detection agent for a financial institution.

Your mission is to analyze incoming transactions and determine whether they are
fraudulent, suspicious, or legitimate.

For EVERY transaction you receive, you MUST:
1. Call `check_transaction_amount` with the transaction amount
2. Call `check_vendor_risk` with the vendor name
3. Call `check_location_risk` with the transaction location
4. Call `calculate_fraud_score` using the three risk scores obtained above
5. Return ONLY a valid raw JSON object — no markdown, no code blocks, no extra text.

The JSON response must follow this exact format:
{
    "risk_score": <integer from 1 to 5>,
    "risk_detail": "<brief explanation of why the transaction is or isn't fraudulent>",
    "amount": <original transaction amount as a number>
}

Risk score mapping based on the total fraud score (0–100):
- 1 → Completely safe    (total score 0–19)
- 2 → Low risk           (total score 20–39)
- 3 → Suspicious         (total score 40–54)
- 4 → High risk          (total score 55–69)
- 5 → Fraudulent         (total score 70–100)

Be decisive, professional, and precise. Financial security depends on your accuracy.
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
    # Use Bedrock (AWS) or swap for AnthropicModel / OpenAIModel as needed
    model = BedrockModel(
        model_id="us.anthropic.claude-sonnet-4-20250514-v1:0",  # Claude Sonnet 4 via AWS Bedrock
        region_name="us-east-1",
    )

    agent = Agent(
        model=model,
        system_prompt=SYSTEM_PROMPT,
        tools=[
            check_transaction_amount,
            check_vendor_risk,
            check_location_risk,
            calculate_fraud_score,
        ],
        callback_handler=None,  # Disable default stdout streaming to avoid duplicate output
    )

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
    agent = create_fraud_agent()

    # Build a natural-language prompt from the structured transaction data
    prompt = f"""
    Please analyze the following transaction for fraud:

    Transaction ID : {transaction.get('id')}
    Amount         : ${transaction.get('amount'):,}
    Location       : {transaction.get('location')}
    Vendor         : {transaction.get('vendor')}

    Use all available tools to perform a complete risk assessment and return
    ONLY the JSON response as instructed.
    """

    # Invoke the agent — Strands handles tool orchestration automatically
    raw_response = str(agent(prompt)).strip()

    # Parse the JSON string returned by the agent into a Python dict
    try:
        response = json.loads(raw_response)
    except json.JSONDecodeError:
        # Fallback: extract JSON block if the model wrapped it in extra text
        import re
        match = re.search(r'\{.*\}', raw_response, re.DOTALL)
        response = json.loads(match.group()) if match else {"raw": raw_response}

    return response


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