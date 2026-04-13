# Fraud Detection - AWS Lambda Durable Functions

Sistema de deteccion de fraude construido sobre **AWS Lambda Durable Functions** y **Amazon Bedrock AgentCore**. Demo de referencia basado en un [blog post de AWS](https://aws.amazon.com/es/blogs/compute/best-practices-for-lambda-durable-functions-using-a-fraud-detection-example/).

---

## Que hace

Procesa transacciones financieras en un flujo de trabajo con multiples pasos, evaluando el riesgo de fraude con un agente de IA:

```
Transaccion -> Agente evalua riesgo (1-5) -> Decision
                                             |-- Score < 3  -> AUTORIZAR automaticamente
                                             |-- Score >= 5 -> ESCALAR a departamento de fraude
                                             +-- Score 3-4  -> Verificacion humana (email + SMS en paralelo)
                                                               |-- Si alguno aprueba -> AUTORIZAR
                                                               +-- Si ambos fallan  -> ESCALAR
```

---

## Componentes principales

| Componente | Ubicacion | Descripcion |
|---|---|---|
| **Lambda Durable Function** | `FraudDetection-Lambda/` | Orquesta todo el flujo. Usa el SDK de ejecucion durable para persistir estado entre pasos (puede correr hasta 1 año) |
| **Agente de Fraude** | `FraudDetection-Agent/` | Servicio FastAPI con un agente LLM (Strands SDK + Claude Sonnet) que analiza transacciones con 4 herramientas: monto, riesgo de vendedor, riesgo de ubicacion, y calculo final |
| **Templates SAM** | `template-with-agent-bedrock.yaml` / `template-without-agent-bedrock.yaml` | Infraestructura como codigo (CloudFormation) |
| **Scripts de despliegue** | `deploy-sam.sh`, `invoke-function.sh`, `send-callback.sh` | Automatizacion de deploy y pruebas |

---

## Arquitectura del flujo

### 1. Evaluacion de riesgo

La Lambda recibe una transaccion y llama al agente de IA para obtener un score de riesgo (1-5).

### 2. Enrutamiento por score

- **Score < 3 (Bajo riesgo):** Se autoriza automaticamente la transaccion.
- **Score >= 5 (Alto riesgo):** Se escala directamente al departamento de fraude.
- **Score 3-4 (Riesgo medio):** Se inicia verificacion humana.

### 3. Verificacion humana (riesgo medio)

1. La transaccion se suspende (el estado se guarda como checkpoint).
2. Se envian notificaciones en paralelo (email via SNS + SMS simulado).
3. Lambda se pausa sin consumir computo.
4. Al recibir un callback humano:
   - Si alguno aprueba -> se autoriza la transaccion.
   - Si ambos rechazan -> se escala a fraude.

---

## Agente de fraude (Strands SDK)

El agente en `FraudDetection-Agent/agent_fraud_detection.py` usa el Strands SDK con 4 herramientas secuenciales:

| Herramienta | Rango de puntos | Criterio |
|---|---|---|
| `check_transaction_amount()` | 0-50 pts | Montos > $5,000 son sospechosos |
| `check_vendor_risk()` | 0-30 pts | Categorias de alto riesgo: electronica, crypto, gift cards |
| `check_location_risk()` | 0-20 pts | Ciudades de alto riesgo: Miami, LA, NY, Las Vegas |
| `calculate_fraud_score()` | 0-100 pts | Agrega puntuaciones y genera veredicto final |

### Mapeo de score interno a score de salida

| Score interno | Score de salida | Nivel |
|---|---|---|
| 0-19 | 1 | Seguro |
| 20-39 | 2 | Riesgo bajo |
| 40-54 | 3 | Sospechoso |
| 55-69 | 4 | Riesgo alto |
| 70-100 | 5 | Fraudulento |

---

## Conceptos clave demostrados

1. **Ejecucion Durable** - Cada paso se guarda como checkpoint. Si Lambda se interrumpe, reanuda desde donde quedo (sin perder estado ni cobrar computo mientras espera).

2. **Agente de IA con herramientas** - El agente usa Strands SDK con 4 tools que evaluan distintas dimensiones de riesgo y producen un score agregado.

3. **Human-in-the-loop** - Para riesgo medio, el flujo se pausa, envia notificaciones paralelas y espera callbacks humanos antes de decidir.

4. **Dos modos de despliegue** - Con Bedrock AgentCore (contenedor Docker gestionado por AWS) o sin el (apuntando a un endpoint HTTP externo).

---

## Stack tecnologico

- **Python 3.12** (Lambda) / **Python 3.11** (Agente)
- **AWS SAM** para infraestructura (CloudFormation)
- **Strands Agents SDK** + **Amazon Bedrock** (Claude Sonnet) para el agente de IA
- **FastAPI + Uvicorn** para el servicio del agente
- **UV** como gestor de dependencias
- **Docker** (ARM64/Graviton) para el contenedor del agente

---

## Estructura del proyecto

```
fraud-detection-aws-lambda-durable-functions/
├── FraudDetection-Lambda/                    # Lambda durable function
│   ├── app.py                                # Handler principal con logica durable
│   ├── test_app.py                           # Tests unitarios
│   ├── pyproject.toml                        # Dependencias (UV)
│   ├── requirements.txt                      # Dependencias compiladas
│   └── uv.lock                               # Versiones fijadas
│
├── FraudDetection-Agent/                     # Servicio del agente de fraude
│   ├── agent.py                              # Servidor FastAPI (/invocations, /ping)
│   ├── agent_fraud_detection.py              # Agente Strands con tools de fraude
│   ├── Dockerfile                            # Imagen Docker ARM64
│   ├── pyproject.toml                        # Dependencias del agente
│   ├── uv.lock                               # Versiones fijadas
│   ├── test-payload.json                     # Payloads de prueba
│   └── README.md                             # Documentacion del agente
│
├── template-with-agent-bedrock.yaml          # SAM template con Bedrock AgentCore
├── template-without-agent-bedrock.yaml       # SAM template sin AgentCore
├── samconfig.toml                            # Configuracion de SAM CLI
│
├── deploy-sam.sh                             # Script de despliegue principal
├── invoke-function.sh                        # Script para probar transacciones
├── send-callback.sh                          # Script para callbacks de verificacion
└── update-function.sh                        # Script para actualizar la funcion
```

---

## Variables de entorno

| Variable | Descripcion |
|---|---|
| `USE_BEDROCK_AGENTCORE` | Usar Bedrock AgentCore (true/false) |
| `AGENT_BASE_URL` | URL del agente HTTP externo |
| `AGENT_RUNTIME_ARN` | ARN del runtime de Bedrock AgentCore |
| `AGENT_REGION` | Region de AWS para Bedrock |
| `SNS_TOPIC` | Topic SNS para notificaciones |
| `API_BASE_URL` | URL base para links de verificacion |

---

## Configuracion por defecto

- **Funcion Lambda:** `fn-Fraud-Detection`
- **Runtime:** Python 3.12
- **Memoria:** 256 MB
- **Timeout por invocacion:** 120 segundos
- **Timeout de ejecucion durable:** 600 segundos (maximo soportado: 1 año)
- **Retencion de historial:** 7 dias
- **Region:** us-east-1

---

## Scripts de uso

### Desplegar

```bash
./deploy-sam.sh
```

### Probar una transaccion

```bash
./invoke-function.sh
# Solicita: Transaction ID, Amount, Location, Vendor, Initial Score
```

### Enviar callback de verificacion

```bash
./send-callback.sh
# Solicita: Callback ID, Respuesta (1=aprobar, 2=rechazar)
```

### Actualizar solo la funcion Lambda

```bash
./update-function.sh
```
