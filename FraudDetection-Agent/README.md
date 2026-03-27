# 🔍 Fraud Detection Agent — Powered by Strands

A fraud detection service built with **[Strands Agents SDK](https://strandsagents.com/)** and **FastAPI**, running on **AWS Bedrock** (Claude Sonnet 4). The agent analyzes financial transactions and returns a risk score from 1 (safe) to 5 (fraud).

---

## 📁 Project Structure

```
FraudDetection-Agent/
├── agent.py                  # FastAPI server — exposes /invocations endpoint
├── agent_fraud_detection.py  # Strands agent — fraud analysis logic & tools
├── pyproject.toml            # Dependencies managed by uv
├── uv.lock
└── README.md
```

---

## 🧠 How It Works

```
POST /invocations
      │
      ▼
 agent.py                         agent_fraud_detection.py
 ┌─────────────────┐    calls          ┌──────────────────────────────────┐
 │  FastAPI        │ ───────────────►  │  Strands Agent (Claude Sonnet 4) │
 │  /invocations   │                   │                                  │
 │                 │ ◄───────────────  │  Tools:                          │
 │  Returns JSON   │    JSON result    │  1. check_transaction_amount     │
 └─────────────────┘                   │  2. check_vendor_risk            │
                                       │  3. check_location_risk          │
                                       │  4. calculate_fraud_score        │
                                       └──────────────────────────────────┘
```

The agent runs **4 tools sequentially** for every transaction and returns a structured JSON response.

---

## 📦 Files

### `agent_fraud_detection.py`
Contains the Strands agent with 4 fraud detection tools:

| Tool | Description | Max Score |
|---|---|---|
| `check_transaction_amount` | Flags amounts above $5,000 threshold | 50 pts |
| `check_vendor_risk` | Classifies vendor category (electronics, crypto, etc.) | 30 pts |
| `check_location_risk` | Evaluates city-level fraud risk (LA, Miami, NY, etc.) | 20 pts |
| `calculate_fraud_score` | Aggregates scores into a final verdict | 100 pts |

**Risk score output mapping:**

| `risk_score` | Meaning | Internal score range |
|---|---|---|
| 1 | Completely safe | 0 – 19 |
| 2 | Low risk | 20 – 39 |
| 3 | Suspicious — manual review | 40 – 54 |
| 4 | High risk | 55 – 69 |
| 5 | Fraudulent — block immediately | 70 – 100 |

---

### `agent.py`
FastAPI server that exposes two endpoints:

| Method | Endpoint | Description |
|---|---|---|
| `POST` | `/invocations` | Analyze a transaction for fraud |
| `GET` | `/ping` | Health check |

---

## ⚙️ Prerequisites

- Python 3.11+
- [uv](https://astral.sh/uv) installed
- AWS account with Bedrock access enabled for `claude-sonnet-4-20250514`

### Install `uv`
```bash
curl -LsSf https://astral.sh/uv/install.sh | sh
```

### Enable Bedrock Model Access
Go to **AWS Console → Bedrock → Model access** and request access to:
```
Anthropic Claude Sonnet 4
```

---

## 🚀 Setup & Installation

**1. Clone or create the project folder:**
```bash
mkdir FraudDetection-Agent && cd FraudDetection-Agent
```

**2. Initialize the project with uv:**
```bash
uv init .
```

**3. Add dependencies:**
```bash
uv add strands-agents[openai]
uv add strands-agents-tools
uv add fastapi
uv add uvicorn
uv add boto3
```

**4. Configure AWS credentials:**
```bash
export AWS_ACCESS_KEY_ID=your_access_key
export AWS_SECRET_ACCESS_KEY=your_secret_key
export AWS_DEFAULT_REGION=us-east-1
```
> Alternatively, configure via `aws configure` if you have the AWS CLI installed.

**5. Copy both files into the project folder:**
```
FraudDetection-Agent/
├── agent.py
└── agent_fraud_detection.py
```

---

## 🧪 Testing Locally

### Step 1 — Start the FastAPI server
```bash
uv run agent.py
```

Expected output:
```
INFO:     Started server process
INFO:     Waiting for application startup.
INFO:     Application startup complete.
INFO:     Uvicorn running on http://0.0.0.0:8080 (Press CTRL+C to quit)
```

---

### Step 2 — Health check
```bash
curl http://localhost:8080/ping
```

Expected response:
```json
{
  "status": "Fraud Detection Agent is running and healthy.",
  "usage": "POST /invocations with body: {'input': {'id': 1, 'amount': 6500, ...}}"
}
```

---

### Step 3 — Test transactions

**🚨 High risk transaction** (large amount + electronics + Los Angeles):
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": {"id": 3, "amount": 6500, "location": "Los Angeles", "vendor": "Electronics Store"}}'
```
Expected:
```json
{
  "output": {
    "risk_score": 5,
    "risk_detail": "High-value transaction at an electronics store in Los Angeles — exceeds amount threshold, high-risk vendor and location.",
    "amount": 6500
  }
}
```

---

**✅ Safe transaction** (small amount + low-risk vendor + low-risk city):
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": {"id": 7, "amount": 45, "location": "Portland", "vendor": "Coffee Shop"}}'
```
Expected:
```json
{
  "output": {
    "risk_score": 1,
    "risk_detail": "Low-value transaction at a legitimate vendor in a low-risk location.",
    "amount": 45
  }
}
```

---

**⚠️ Suspicious transaction** (medium amount + online gaming + Seattle):
```bash
curl -X POST http://localhost:8080/invocations \
  -H "Content-Type: application/json" \
  -d '{"input": {"id": 12, "amount": 1200, "location": "Seattle", "vendor": "Online Gaming Store"}}'
```
Expected:
```json
{
  "output": {
    "risk_score": 2,
    "risk_detail": "Moderate transaction with medium-risk vendor and location — within acceptable range.",
    "amount": 1200
  }
}
```

---

### Step 4 — Test the agent directly (without FastAPI)
```bash
uv run agent_fraud_detection.py
```
This runs the 3 built-in sample transactions and prints JSON results to stdout.

---

## 🔁 Response Schema

```json
{
  "output": {
    "risk_score": 1,
    "risk_detail": "string — explanation of the fraud assessment",
    "amount": 6500
  }
}
```

| Field | Type | Description |
|---|---|---|
| `risk_score` | `int` | 1 = safe, 5 = fraud |
| `risk_detail` | `str` | Agent explanation |
| `amount` | `float` | Original transaction amount |

---

## ❌ Error Handling

| HTTP Status | Cause |
|---|---|
| `400` | Missing `amount` field in request |
| `500` | Agent processing failed (AWS credentials, model access, etc.) |

Example error response:
```json
{
  "detail": "Amount not provided. Please include 'amount' (in USD) in the request."
}
```

---

## 🛠️ Troubleshooting

**Port already in use:**
```bash
# Check what's using the port
sudo lsof -i :8080

# Use a different port — edit agent.py:
uvicorn.run(app, host="0.0.0.0", port=8000)
```

**AWS credentials error:**
```bash
# Verify credentials are set
aws sts get-caller-identity

# Or re-export them
export AWS_ACCESS_KEY_ID=...
export AWS_SECRET_ACCESS_KEY=...
```

**Bedrock model access denied:**
Go to **AWS Console → Bedrock → Model access** and enable:
`Anthropic → Claude Sonnet 4`
