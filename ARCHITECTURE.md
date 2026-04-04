# Enterprise RAG System on Amazon Bedrock — Architecture Design Document

**Date:** 2026-04-04
**Status:** Pre-Implementation Design
**Scope:** Production-grade Retrieval-Augmented Generation using Amazon Bedrock Knowledge Bases

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Component Breakdown](#2-component-breakdown)
3. [Data Flow](#3-data-flow)
4. [AWS Services Inventory](#4-aws-services-inventory)
5. [Key Architectural Decisions and Justifications](#5-key-architectural-decisions-and-justifications)
6. [Real-World Constraints](#6-real-world-constraints)
7. [What a Naive Implementation Gets Wrong](#7-what-a-naive-implementation-gets-wrong)
8. [IAM Security Model](#8-iam-security-model)
9. [Cost Model](#9-cost-model)
10. [Latency Budget](#10-latency-budget)
11. [Project Folder Structure](#11-project-folder-structure)
12. [Python Dependencies](#12-python-dependencies)
13. [Observability Strategy](#13-observability-strategy)
14. [Failure Modes and Mitigations](#14-failure-modes-and-mitigations)

---

## 1. System Overview

This system provides enterprise-grade question answering over a private document corpus using a fully managed RAG pipeline on AWS. It is designed for:

- Multi-tenant document ingestion (PDFs, DOCX, HTML, Markdown, CSV)
- Semantic retrieval over large corpora (100K+ documents)
- Grounded, citation-backed answers via Amazon Bedrock LLMs
- Auditability, PII handling, and compliance logging
- Horizontal scalability without managing vector DB infrastructure

The guiding principle: **own the business logic, not the infrastructure plumbing.**

---

## 2. Component Breakdown

### 2.1 Ingestion Pipeline

**Responsibility:** Accept raw documents, transform them into indexed vector embeddings in the Bedrock Knowledge Base.

| Sub-component | Technology | Role |
|---|---|---|
| Document Source | S3 (raw bucket) | Landing zone for all source documents |
| Trigger | S3 Event Notification -> SQS | Decoupled, reliable trigger on new uploads |
| Pre-processor | Lambda (Python) | Sanitization, metadata tagging, PII scrubbing |
| Chunker | Bedrock KB (managed) | Fixed/semantic chunking with overlap |
| Embedder | Bedrock Titan Embeddings v2 | 1536-dim embeddings, managed |
| Vector Store | OpenSearch Serverless (AOSS) | Managed by Bedrock KB, k-NN index |
| Metadata Store | DynamoDB | Document-level metadata: source, tenant, date, version |

### 2.2 Retrieval and Generation (Query Path)

**Responsibility:** Accept user queries, retrieve relevant chunks, generate grounded answers.

| Sub-component | Technology | Role |
|---|---|---|
| API Gateway | API Gateway (HTTP API) | HTTPS entry point, request throttling |
| Auth | Cognito / API Gateway Authorizer | JWT validation, tenant identity extraction |
| Query Handler | Lambda (Python) | Orchestration: query rewriting, KB call, response assembly |
| Query Rewriter | Bedrock Claude (optional) | Reformulates ambiguous queries before retrieval |
| Knowledge Base | Bedrock Knowledge Base | Hybrid semantic + keyword retrieval |
| Re-ranker | Bedrock Rerank API (Cohere Rerank) | Scores retrieved chunks by relevance before generation |
| Generator | Bedrock Claude 3.5 Sonnet | Final answer synthesis with citations |
| Response Cache | ElastiCache (Redis) | TTL-based cache for repeated queries |
| Audit Logger | Kinesis Data Firehose -> S3 | Immutable audit trail of every query/response |

### 2.3 Administration and Ops

| Sub-component | Technology | Role |
|---|---|---|
| Ingestion Job Monitor | Step Functions | Orchestrate multi-step sync jobs with retry/error handling |
| KB Sync Trigger | EventBridge Scheduler | Scheduled full/incremental sync jobs |
| Config Store | AWS Systems Manager Parameter Store | Runtime config: model IDs, chunk sizes, top-K values |
| Secrets | AWS Secrets Manager | API keys, third-party credentials |
| Monitoring | CloudWatch Logs, Metrics, Alarms | Latency, error rate, token usage |
| Tracing | AWS X-Ray | Distributed trace across Lambda -> Bedrock -> AOSS |

---

## 3. Data Flow

### 3.1 Ingestion Flow

```
[Document Upload]
    -> S3 (raw-documents/{tenant_id}/{doc_id})
    -> S3 Event Notification
    -> SQS Queue (with DLQ)
    -> Lambda: pre-processor
        - Validate file type and size
        - Extract/enrich metadata (source, language, classification)
        - PII detection (Comprehend) and redaction if required
        - Write metadata to DynamoDB
        - Copy sanitized document to S3 (processed-documents/)
    -> Bedrock KB Sync (StartIngestionJob API)
        - Chunking (semantic, 512 tokens, 20% overlap)
        - Titan Embeddings v2 (1536-dim)
        - Upsert into OpenSearch Serverless k-NN index
    -> Step Functions: monitor sync job status
    -> EventBridge: emit ingestion-complete event
    -> SNS: notify downstream consumers (optional)
```

### 3.2 Query Flow

```
[User Query via HTTPS]
    -> API Gateway (HTTP API)
    -> Cognito Authorizer (validate JWT, extract tenant_id)
    -> Lambda: query-handler
        1. Extract tenant_id, session_id, raw_query
        2. Check Redis cache (hash of query + tenant) -> if HIT, return cached response
        3. [Optional] Query Rewrite via Claude: clarify ambiguous language
        4. Call Bedrock KB: RetrieveAndGenerate OR Retrieve (2-step)
           - numberOfResults: 10-20 (retrieve more, rerank down)
           - filter: {"equals": {"key": "tenant_id", "value": tenant_id}}
           - retrievalMode: HYBRID (semantic + BM25)
        5. [If 2-step] Pass chunks to Bedrock Rerank API
           - Reduce to top-5 most relevant chunks
        6. [If 2-step] Assemble prompt with ranked context
        7. Call Bedrock InvokeModel: Claude 3.5 Sonnet
           - System prompt: grounding instructions, citation format
           - Temperature: 0.0 (factual answers only)
        8. Extract citations, format response
        9. Write to Redis cache (TTL: 300s for common queries)
        10. Emit audit event to Kinesis Firehose
        11. Return JSON: {answer, citations, session_id, latency_ms}
```

### 3.3 Multi-Tenancy Isolation

Every document stored in the KB carries a `tenant_id` metadata attribute. Every query applies a mandatory metadata filter on `tenant_id`. This is enforced at the Lambda layer — the user cannot override it because the filter is injected server-side from the validated JWT, never from the request body.

---

## 4. AWS Services Inventory

| Service | Usage | Why This, Not Alternatives |
|---|---|---|
| **Bedrock Knowledge Bases** | Managed RAG pipeline (embed + index + retrieve) | Eliminates vector DB ops burden; native Bedrock integration |
| **OpenSearch Serverless** | Vector store (managed by Bedrock KB) | Auto-scales; k-NN + BM25 hybrid; no cluster management |
| **Bedrock Titan Embeddings v2** | Embedding model | Same provider as KB; no cross-account latency; FIPS endpoints |
| **Bedrock Claude 3.5 Sonnet** | Answer generation | Best instruction-following at enterprise scale; citation support |
| **Bedrock Rerank (Cohere)** | Re-ranking retrieved chunks | Measurably improves answer precision at low latency cost |
| **S3** | Document storage (raw + processed) | Durable, cheap, native Bedrock KB data source |
| **SQS** | Decoupled ingestion trigger | Prevents Lambda timeout on burst upload; DLQ for failures |
| **Lambda** | Compute for pre-processing and query handling | Serverless; no idle cost; scales to zero |
| **API Gateway (HTTP API)** | HTTPS entry point | Cheaper than REST API; built-in throttling and CORS |
| **Cognito** | Auth and tenant identity | Managed; integrates with API GW authorizer |
| **DynamoDB** | Document metadata, session state | Single-digit ms reads; on-demand billing |
| **ElastiCache (Redis)** | Query response cache | Sub-ms reads; reduces Bedrock token spend on repeated queries |
| **Step Functions** | Ingestion job orchestration | Retry logic, branching, error handling without custom code |
| **Kinesis Firehose** | Audit log delivery | Buffered, fault-tolerant delivery to S3; no log loss |
| **CloudWatch** | Metrics, alarms, logs | Native AWS; no additional agent needed |
| **X-Ray** | Distributed tracing | Native Lambda and SDK integration |
| **SSM Parameter Store** | Runtime config | Free tier for standard params; versioned; no restart needed |
| **Secrets Manager** | Credentials | Auto-rotation; IAM-gated access |
| **Comprehend** | PII detection during ingestion | Managed NLP; no ML model to maintain |
| **KMS** | Encryption at rest for S3, DDB, AOSS | Compliance requirement; customer-managed keys |

---

## 5. Key Architectural Decisions and Justifications

### Decision 1: Bedrock Knowledge Bases vs. Custom LangChain + Pinecone/Weaviate

**Chose Bedrock KB.**

Reasons:
- A custom stack (LangChain + self-managed vector DB) means owning: embedding pipeline reliability, vector DB scaling, index backup/restore, chunking consistency, SDK version drift, and a separate infra cost center.
- Bedrock KB handles all of this. The chunking, embedding, and indexing are atomic — you don't debug partial states.
- The Bedrock KB Retrieve API returns citations (source URI + chunk text) natively. Custom stacks need this built from scratch.
- For enterprises: Bedrock KB stores data within your VPC-adjacent AOSS, which satisfies data residency requirements.

**Trade-off accepted:** Less control over the chunking algorithm and embedding model. Mitigated by: careful chunk size tuning and using the 2-step Retrieve + InvokeModel flow instead of RetrieveAndGenerate when you need full control over the prompt.

### Decision 2: RetrieveAndGenerate API vs. 2-Step Retrieve + InvokeModel

**Chose 2-Step for production.**

`RetrieveAndGenerate` is a convenience API. In production:
- You cannot inject dynamic system prompts (guardrails, tenant-specific instructions).
- You cannot add a re-ranking step between retrieve and generate.
- You cannot implement query rewriting.
- You cannot cache the final response.
- Error messages from the LLM are opaque.

The 2-step approach (Retrieve -> Rerank -> InvokeModel) adds ~50ms latency but gives full control. **Use RetrieveAndGenerate only for demos.**

### Decision 3: Hybrid Retrieval (Semantic + BM25) Over Pure Semantic

Pure semantic retrieval fails on:
- Exact product names, model numbers, codes (e.g., "SKU-4892-B")
- Acronyms and domain jargon (semantic embeddings may not capture these)
- Queries that are intentionally keyword-specific

BM25 (keyword) retrieval catches these exact matches. Hybrid mode is the correct default. OpenSearch Serverless supports both natively via the Bedrock KB hybrid retrieval mode.

### Decision 4: Cohere Rerank as a Step Between Retrieve and Generate

Retrieve 15 chunks, rerank to 5. This is the highest-ROI improvement in a RAG system after the initial setup. Without reranking:
- Retrieved chunks are ranked by cosine similarity, which does not account for semantic relevance to the specific query nuance.
- Sending 15 chunks to the LLM increases token cost and context noise (degrading answer quality via "lost in the middle" effect).

Reranking with Cohere Rerank via Bedrock adds ~100ms and costs fractions of a cent, but measurably improves answer correctness.

### Decision 5: Metadata Filtering for Multi-Tenancy (Not Separate Knowledge Bases)

Two approaches exist: one KB per tenant, or one KB with metadata filters.

One KB per tenant: simpler isolation, but O(N) management overhead (KB creation, sync jobs, monitoring) as tenant count grows.

One KB + metadata filter: operationally simpler, scales to hundreds of tenants. The risk is filter bypass — mitigated by injecting the filter server-side from the JWT (never from user input).

**Chose shared KB with server-side metadata filtering.**

---

## 6. Real-World Constraints

### 6.1 Cost Constraints

| Cost Driver | Approx. Unit Cost | Risk |
|---|---|---|
| Titan Embeddings v2 | $0.00002/1K tokens | Ingestion bursts can be expensive |
| Claude 3.5 Sonnet | $3/$15 per 1M in/out tokens | Token-heavy prompts = runaway cost |
| OpenSearch Serverless | $0.24/OCU-hour (min 2 OCU) | Minimum ~$350/month even at zero load |
| API Gateway | $1/million requests | Negligible at enterprise scale |
| ElastiCache | ~$25/month (cache.t4g.micro) | Fixed cost, high ROI |

**Critical:** OpenSearch Serverless has a minimum billing floor of 2 OCUs for indexing + 2 OCUs for search = ~$700/month minimum. This is non-negotiable. If budget is under $700/month for infra, consider Bedrock KB with a different vector store backend (not AOSS) or accept this as the baseline cost.

### 6.2 Latency Constraints

Target P99 latency: < 5 seconds for a complete RAG query response.

Breakdown (see Section 10 for full budget).

### 6.3 Bedrock Service Quotas

- Bedrock has per-region, per-model throttling limits (Transactions Per Minute, TPM).
- Default Claude 3.5 Sonnet quota: 50 RPM on-demand. This is dangerously low for production.
- **Action required:** Request quota increases via AWS Service Quotas BEFORE going live.
- Use Provisioned Throughput if you need guaranteed capacity (significant cost uplift, but removes throttling risk).

### 6.4 Ingestion Latency

Bedrock KB ingestion is NOT real-time. The `StartIngestionJob` API initiates an async job. A full sync on a large corpus can take 10-60 minutes. Design the system to communicate document processing status to users explicitly. Do not promise immediate searchability.

### 6.5 Context Window Limits

Claude 3.5 Sonnet: 200K token context window. This sounds large, but:
- Each retrieved chunk: ~512 tokens
- 10 chunks: 5,120 tokens of context
- System prompt + instruction: ~500 tokens
- User query: ~50-200 tokens
- Expected output: ~500-1,500 tokens
- Total: well within 200K for normal use

The risk is prompt injection via document content. Enforce guardrails to prevent retrieved text from overriding system instructions.

---

## 7. What a Naive Implementation Gets Wrong

### 7.1 Chunking Without Overlap

Fixed-size chunking without overlap means a sentence can be split across two chunks. The answer to a question may span the boundary, and neither chunk alone will be retrieved. **Always use 15-20% overlap.**

### 7.2 Not Filtering by Tenant at Query Time

Naive implementations forget to apply metadata filters. A user from Tenant A can retrieve documents from Tenant B. This is a critical security flaw. The filter must be injected server-side.

### 7.3 Using `RetrieveAndGenerate` in Production

As described in Decision 2. It hides latency, prevents caching, prevents reranking, and prevents dynamic system prompt injection.

### 7.4 Not Handling KB Sync Failures

`StartIngestionJob` can fail silently for individual documents (malformed PDF, unsupported encoding). Naive implementations assume everything synced. The KB sync API returns per-document failure records — these must be parsed and alerted on.

### 7.5 Embedding User Queries Without Preprocessing

Raw user queries are noisy (typos, stopwords, abbreviations). Without query normalization or optional query rewriting, retrieval quality degrades for casual user input. At minimum: strip leading/trailing whitespace, normalize unicode, and set a max query length.

### 7.6 No Response Caching

Every call to Bedrock costs tokens. In enterprise settings, the same question is asked by multiple users (e.g., "What is our PTO policy?"). Without a cache layer, you pay full token cost every time. A Redis cache keyed on `sha256(tenant_id + normalized_query)` with a 5-minute TTL eliminates the majority of redundant spend.

### 7.7 Ignoring Cold Start on Lambda

The query handler Lambda will cold-start on the first request after idle. This adds 500ms-2s. For a production API, configure Provisioned Concurrency on the query handler Lambda to eliminate cold starts.

### 7.8 Treating Bedrock as Infallible

Bedrock endpoints return throttling errors (`ThrottlingException`), transient service errors, and model-specific errors. Every Bedrock call must be wrapped in exponential backoff with jitter. The boto3 `botocore` retry configuration defaults are insufficient for LLM workloads.

### 7.9 Storing Raw Documents Only in the KB Data Source

If the S3 source document is deleted or modified, the KB index becomes stale or inconsistent. Always maintain a separate canonical document store (DynamoDB metadata + S3 versioning enabled) independent of what Bedrock KB indexes.

### 7.10 Not Versioning Prompts

System prompts change over time. Naive implementations hardcode prompts in Lambda code. Changes require a deployment. Use SSM Parameter Store for prompts — changes take effect immediately, are versioned, and can be rolled back without a code deployment.

---

## 8. IAM Security Model

### Principle: Least Privilege at Every Boundary

```
[User]
  -> Cognito User Pool (authentication)
  -> API Gateway (request authorization via Cognito Authorizer)

[API Gateway]
  -> Lambda Execution Role (query-handler-role)
     Permissions:
       - bedrock:Retrieve (on specific KB ARN)
       - bedrock:InvokeModel (on specific model ARNs only)
       - bedrock:Rerank
       - dynamodb:GetItem, PutItem (on specific table ARNs)
       - elasticache:Connect (via VPC security group, not IAM)
       - ssm:GetParameter (on /rag/* path only)
       - xray:PutTraceSegments
       - kinesis:PutRecord (on specific Firehose ARN)
       DENY: bedrock:CreateKnowledgeBase, bedrock:DeleteKnowledgeBase
       DENY: s3:* (query handler has NO direct S3 access)

[S3 Trigger]
  -> Lambda Execution Role (ingestion-preprocessor-role)
     Permissions:
       - s3:GetObject (source bucket only)
       - s3:PutObject (processed bucket only)
       - comprehend:DetectPiiEntities
       - dynamodb:PutItem, UpdateItem
       - bedrock:StartIngestionJob (on specific KB ARN)
       - sqs:ReceiveMessage, DeleteMessage (on specific queue ARN)

[Bedrock KB Service Role] (assumed by Bedrock, not your code)
     Permissions:
       - s3:GetObject (processed documents bucket)
       - aoss:APIAccessAll (on specific AOSS collection)
       - bedrock:InvokeModel (Titan Embeddings only)

[Step Functions Role]
     Permissions:
       - bedrock:GetIngestionJob
       - lambda:InvokeFunction (ingestion monitor Lambda only)
       - sns:Publish (for failure alerts)
```

### Additional Security Controls

- **S3 Bucket Policies:** Block public access. Enforce `aws:SecureTransport`. Enable versioning and MFA delete on the raw document bucket.
- **AOSS Access Policies:** Restrict access to KB service role ARN only. No direct developer access to the production AOSS collection.
- **VPC:** Lambda functions run inside a VPC. Bedrock access via VPC Endpoint (PrivateLink). No traffic leaves AWS network.
- **KMS:** All S3 buckets, DynamoDB tables, and AOSS collections use customer-managed KMS keys. The KB service role needs `kms:Decrypt` and `kms:GenerateDataKey` on these keys.
- **Resource-Based Policies:** Bedrock KB itself has a resource policy — ensure only your account's roles can call Retrieve.

---

## 9. Cost Model

### Monthly Estimate: Medium Enterprise Workload

**Assumptions:** 50K documents, 1M queries/month, average 1K tokens in + 500 tokens out per query.

| Component | Cost Driver | Estimated Monthly Cost |
|---|---|---|
| OpenSearch Serverless | 2 indexing + 2 search OCU (minimum) | $700 |
| Titan Embeddings v2 | 50K docs × 1K tokens avg = 50M tokens | $1.00 (one-time ingestion) |
| Claude 3.5 Sonnet | 1M queries × 1K in + 500 out tokens | $3,000 + $7,500 = $10,500 |
| Cohere Rerank | 1M queries × 15 docs × 100 tokens/doc | ~$150 |
| Lambda | 1M invocations, 1s avg | ~$20 |
| API Gateway | 1M requests | $1 |
| ElastiCache (t4g.micro) | Fixed | $25 |
| Kinesis Firehose | 1M records | ~$5 |
| S3 | 50K docs × 1MB avg = 50GB | ~$1.15 |
| **Total** | | **~$11,400/month** |

**Critical optimization:** The Claude token cost dominates. A 60% cache hit rate on Redis (realistic for FAQ-heavy enterprise) reduces the monthly Claude cost to ~$4,200. **Invest in caching.**

Also: use Claude Haiku for simple/factual queries and route to Sonnet only for complex reasoning. A query classifier (rule-based or lightweight LLM call) can route cheaply.

---

## 10. Latency Budget

**Target: P99 < 5,000ms for end-to-end query response**

| Step | P50 | P99 | Notes |
|---|---|---|---|
| API Gateway + Auth | 10ms | 30ms | Negligible |
| Lambda init (warm) | 5ms | 20ms | Requires Provisioned Concurrency |
| Redis cache check | 1ms | 5ms | Same-VPC access |
| [Optional] Query rewrite | 300ms | 800ms | Skip for simple queries |
| Bedrock KB Retrieve | 200ms | 600ms | Hybrid retrieval |
| Cohere Rerank | 100ms | 300ms | 15 -> 5 docs |
| Bedrock InvokeModel (Claude) | 800ms | 2,500ms | First-token latency + streaming |
| Response assembly + logging | 10ms | 30ms | Local computation |
| **Total (no rewrite)** | **~1,130ms** | **~3,485ms** | Within 5s budget |
| **Total (with rewrite)** | **~1,430ms** | **~4,285ms** | Still within budget |

**Risk:** Bedrock InvokeModel P99 can spike to 4-6s during regional load events. Use streaming responses (`InvokeModelWithResponseStream`) and begin returning tokens to the user as they arrive. This dramatically improves perceived latency even if total time is the same.

---

## 11. Project Folder Structure

```
enterprise-rag-bedrock/
│
├── ARCHITECTURE.md                    # This document
├── pyproject.toml                     # Project metadata, tool config
├── requirements.txt                   # Pinned production deps
├── requirements-dev.txt               # Dev/test deps
├── .env.example                       # Template for local env vars (never commit .env)
├── Makefile                           # Common dev commands
│
├── infra/                             # Infrastructure as Code (CDK or Terraform)
│   ├── stacks/
│   │   ├── storage_stack.py           # S3, DynamoDB, KMS
│   │   ├── network_stack.py           # VPC, subnets, security groups, VPC endpoints
│   │   ├── auth_stack.py              # Cognito User Pool, App Client
│   │   ├── bedrock_stack.py           # Knowledge Base, Data Source, AOSS collection
│   │   ├── ingestion_stack.py         # SQS, Lambda (preprocessor), Step Functions
│   │   ├── query_stack.py             # Lambda (query handler), API Gateway, ElastiCache
│   │   ├── observability_stack.py     # CloudWatch dashboards, alarms, X-Ray, Firehose
│   │   └── iam_stack.py               # All IAM roles and policies, centralized
│   ├── constructs/
│   │   └── bedrock_kb_construct.py    # Reusable CDK construct for KB + AOSS
│   └── app.py                         # CDK App entry point
│
├── src/
│   ├── __init__.py
│   │
│   ├── ingestion/                     # Ingestion pipeline logic
│   │   ├── __init__.py
│   │   ├── handler.py                 # Lambda entry point (SQS trigger)
│   │   ├── preprocessor.py            # File validation, metadata extraction, PII scrub
│   │   ├── pii_detector.py            # Comprehend PII detection wrapper
│   │   ├── metadata_writer.py         # DynamoDB metadata record writer
│   │   └── kb_sync.py                 # StartIngestionJob wrapper with retry logic
│   │
│   ├── query/                         # Query and generation pipeline logic
│   │   ├── __init__.py
│   │   ├── handler.py                 # Lambda entry point (API Gateway trigger)
│   │   ├── auth.py                    # JWT parsing, tenant_id extraction
│   │   ├── query_rewriter.py          # Optional LLM-based query reformulation
│   │   ├── retriever.py               # Bedrock KB Retrieve API wrapper
│   │   ├── reranker.py                # Bedrock Rerank API wrapper (Cohere)
│   │   ├── generator.py               # Bedrock InvokeModel wrapper (Claude)
│   │   ├── prompt_builder.py          # Assemble system + context + user prompt
│   │   ├── citation_extractor.py      # Parse and format citations from KB response
│   │   └── response_formatter.py      # Final response schema assembly
│   │
│   ├── cache/                         # Caching layer
│   │   ├── __init__.py
│   │   └── redis_cache.py             # Redis get/set with TTL, key hashing
│   │
│   ├── audit/                         # Audit logging
│   │   ├── __init__.py
│   │   └── firehose_logger.py         # Kinesis Firehose audit event publisher
│   │
│   ├── config/                        # Runtime configuration
│   │   ├── __init__.py
│   │   └── settings.py                # SSM Parameter Store loader, typed settings dataclass
│   │
│   └── common/                        # Shared utilities
│       ├── __init__.py
│       ├── bedrock_client.py          # Singleton boto3 Bedrock clients with retry config
│       ├── exceptions.py              # Custom exception hierarchy
│       ├── logging.py                 # Structured JSON logger (aws-lambda-powertools)
│       └── tracing.py                 # X-Ray tracing decorators
│
├── tests/
│   ├── __init__.py
│   ├── conftest.py                    # Pytest fixtures, moto mocks, test config
│   │
│   ├── unit/
│   │   ├── ingestion/
│   │   │   ├── test_preprocessor.py
│   │   │   ├── test_pii_detector.py
│   │   │   └── test_kb_sync.py
│   │   └── query/
│   │       ├── test_retriever.py
│   │       ├── test_reranker.py
│   │       ├── test_generator.py
│   │       ├── test_prompt_builder.py
│   │       └── test_response_formatter.py
│   │
│   ├── integration/
│   │   ├── test_ingestion_flow.py     # End-to-end ingestion with localstack
│   │   └── test_query_flow.py         # End-to-end query with mocked Bedrock
│   │
│   └── load/
│       └── locustfile.py              # Locust load test: ramp to 100 concurrent users
│
├── scripts/
│   ├── bootstrap_kb.py                # One-time KB creation + AOSS index setup
│   ├── bulk_ingest.py                 # Bulk upload documents from a local directory
│   ├── run_eval.py                    # RAG evaluation: faithfulness, relevance, recall
│   └── rotate_prompts.py              # Push updated prompts to SSM Parameter Store
│
├── prompts/                           # Versioned prompt templates (source of truth)
│   ├── system_prompt_v1.txt
│   └── query_rewrite_prompt_v1.txt
│
└── docs/
    ├── runbook.md                     # On-call runbook: common failure modes + remediation
    ├── quota_guide.md                 # Bedrock quota limits + how to request increases
    └── cost_optimization.md           # Cost optimization playbook
```

---

## 12. Python Dependencies

### 12.1 `requirements.txt` (Production)

```
# AWS SDK
boto3==1.38.0
botocore==1.38.0

# AWS Lambda Powertools — structured logging, tracing, metrics, parameter store
aws-lambda-powertools==3.6.0

# Redis client for ElastiCache
redis==5.2.1
hiredis==3.1.0          # C-accelerated Redis protocol parser (2-5x faster)

# Pydantic — request/response validation, typed settings
pydantic==2.11.0
pydantic-settings==2.9.0

# Tenacity — robust retry logic with exponential backoff + jitter
tenacity==9.1.0

# PyJWT — JWT parsing for tenant identity extraction
PyJWT==2.10.1
cryptography==44.0.2    # JWT signature verification

# Structlog — structured logging (complements powertools)
structlog==25.1.0

# Tiktoken — accurate token counting before Bedrock calls
tiktoken==0.9.0
```

### 12.2 `requirements-dev.txt` (Development and Testing)

```
# Testing
pytest==8.3.5
pytest-asyncio==0.25.3
pytest-cov==6.1.0
pytest-mock==3.14.0
moto[s3,sqs,dynamodb,kinesis,ssm,comprehend]==5.1.0   # AWS service mocks
responses==0.25.3        # HTTP mock for non-boto calls

# Load testing
locust==2.33.0

# Type checking
mypy==1.13.0
boto3-stubs[bedrock,bedrock-agent,bedrock-agent-runtime,s3,sqs,dynamodb,comprehend,ssm,kinesis-firehose]==1.38.0

# Linting and formatting
ruff==0.9.10
black==25.1.0

# Security scanning
bandit==1.8.3
pip-audit==2.9.0

# RAG evaluation
ragas==0.2.15            # Faithfulness, answer relevancy, context recall metrics
deepeval==2.3.0          # Additional LLM evaluation metrics

# Local development
python-dotenv==1.0.1
localstack-cli==4.0.0
```

### 12.3 Key Dependency Decisions

**Why `aws-lambda-powertools` and NOT custom logging?**
Powertools gives you structured JSON logging, X-Ray tracing decorators, SSM parameter caching, idempotency utilities, and event parsing — all battle-tested at AWS scale. Writing these from scratch wastes weeks.

**Why `tenacity` and NOT custom retry loops?**
Tenacity handles exponential backoff with jitter, configurable stop conditions, and retry callbacks in a composable decorator pattern. `botocore` retries are for transient HTTP errors, not for semantic errors like `ThrottlingException` on Bedrock, which needs application-level retry logic.

**Why `tiktoken` and NOT estimating tokens?**
Bedrock billing is per token. Over-sending context wastes money; under-sending truncates answers. Count tokens accurately before each Bedrock call to enforce hard limits and budget guardrails.

**Why `ragas` and `deepeval`?**
You cannot know if your RAG pipeline is working without evaluation. These libraries provide: faithfulness (is the answer grounded in the retrieved context?), answer relevancy (does it answer the question?), and context recall (did we retrieve the right chunks?). Run evaluations on a golden dataset before every deployment.

---

## 13. Observability Strategy

### 13.1 Metrics to Track (CloudWatch Custom Metrics)

| Metric | Alarm Threshold | Action |
|---|---|---|
| `QueryLatencyP99` | > 4,500ms | PagerDuty alert |
| `BedrockThrottleCount` | > 10/min | Alert + auto-scale Provisioned Throughput |
| `IngestionJobFailureCount` | > 0 | Alert + SNS to doc owners |
| `CacheHitRate` | < 30% | Alert — investigate query distribution |
| `BedrockTokensConsumed` | > budget threshold | Alert — cost control |
| `LambdaErrorRate` | > 1% | Alert |
| `RetrievedChunkCount` | avg < 3 | Alert — KB may be stale or misconfigured |

### 13.2 Structured Log Schema

Every log entry emits JSON with these mandatory fields:
```json
{
  "timestamp": "ISO-8601",
  "request_id": "uuid",
  "tenant_id": "string",
  "session_id": "uuid",
  "event_type": "QUERY | INGEST | ERROR",
  "latency_ms": 1234,
  "model_id": "anthropic.claude-3-5-sonnet-20241022-v2:0",
  "input_tokens": 1024,
  "output_tokens": 512,
  "cache_hit": false,
  "retrieved_chunks": 5,
  "level": "INFO | WARNING | ERROR"
}
```

### 13.3 Audit Trail (Firehose -> S3)

Every query and its response is written to Kinesis Firehose within the Lambda. Firehose buffers and delivers to S3 with Snappy compression. Partitioned by `year/month/day/tenant_id`. Athena-queryable for compliance reviews.

---

## 14. Failure Modes and Mitigations

| Failure Mode | Detection | Mitigation |
|---|---|---|
| Bedrock ThrottlingException | X-Ray trace, CloudWatch error rate | Exponential backoff (tenacity); request quota increase; Provisioned Throughput |
| KB sync job fails for a document | Step Functions task failure | Retry 3x; write failure to DynamoDB; alert via SNS; document stays in "pending" state |
| AOSS collection unhealthy | CloudWatch AOSS metrics | Automatic AOSS recovery is managed; alert on query failure rate spike |
| Redis cache miss storm (cold start) | Cache hit rate metric drop | Pre-warm cache on deploy with top-N popular queries |
| Lambda cold start spike | P99 latency alarm | Provisioned Concurrency on query handler (3-5 instances) |
| PII leaked into index | Comprehend detection logs | Pre-ingestion PII scrubbing; post-retrieval output scanning via Bedrock Guardrails |
| Tenant filter bypass | Security audit log review | Server-side filter injection enforced in Lambda; integration test asserts cross-tenant isolation |
| Context stuffing / prompt injection | Bedrock Guardrails | Enable Bedrock Guardrails with deny topics and sensitive word blocking |
| Document deleted but still indexed | KB returns stale chunk | S3 versioning + KB re-sync on delete; DynamoDB soft delete with TTL |
| Runaway token cost | CloudWatch billing alarm | Per-tenant token budget enforced in Lambda; hard cutoff with 429 response |
```
