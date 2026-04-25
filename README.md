# Enterprise Knowledge Base Q&A — Amazon Bedrock RAG

A question-answering system that lets employees ask natural language questions about internal company documents. Built on Amazon Bedrock Knowledge Bases (managed RAG) with a Streamlit frontend.

---

## Demo

[![Demo Video](https://img.shields.io/badge/Watch%20Demo-Google%20Drive-blue?logo=googledrive)](https://drive.google.com/file/d/1YqUxSr7n8lGeUhHvApju1E2UE3MOSfoy/view)

The demo shows the RAG pipeline in action — asking a question before uploading a document (no answer), then uploading the document and asking the same question (grounded answer with source citation). This proves the system retrieves from your actual documents, not from the LLM's training data.

---

## What this does

- Upload PDFs, Word docs, or text files through the UI
- They get stored in S3 and indexed by Bedrock (chunk, embed, store in OpenSearch Serverless)
- Ask questions in plain English
- Get answers with citations pointing back to the source documents

The core idea is RAG: instead of relying on the LLM's built-in training knowledge, you first retrieve relevant document chunks from your own data, then pass them as context to the model. This keeps answers grounded in your actual documents and avoids hallucination on company-specific content.

---

## Architecture

```
Documents → S3 → Bedrock Ingestion → OpenSearch Serverless (vectors)
                                            ↓
User Query → Bedrock Retrieve (hybrid search) → Top-K chunks
                                            ↓
                              Amazon Nova Lite (generate answer)
                                            ↓
                                    Answer + citations
```

Key design choice: using the 2-step **Retrieve + InvokeModel** flow instead of the `RetrieveAndGenerate` API. The built-in API is convenient for demos but doesn't let you control the system prompt, inspect retrieved chunks, or add reranking. The 2-step approach costs a bit more to implement but gives you actual control.

Retrieval uses hybrid mode (semantic + keyword). Pure semantic search struggles with exact product codes, acronyms, or names — BM25 handles those cases.

---

## Setup

### Prerequisites

- AWS account with Bedrock access (Amazon Nova Lite + Titan Embeddings v2 enabled in your region)
- Python 3.11+
- IAM user or EC2 role with: `bedrock:*`, `s3:*`, `iam:CreateRole`, `iam:PutRolePolicy`

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Configure environment

```bash
cp .env.example .env
# Edit .env — at minimum set S3_BUCKET_NAME
```

### 3. Run one-time setup (creates KB + S3 bucket)

```bash
python setup_kb.py
```

This creates the S3 bucket, IAM role for Bedrock, Knowledge Base, and data source. It prints the `KNOWLEDGE_BASE_ID` and `DATA_SOURCE_ID` at the end — add those to `.env`.

Takes about 5–10 minutes because of IAM propagation and KB initialization.

### 4. Start the app

```bash
streamlit run app.py
```

---

## Deploying on EC2

```bash
# SSH into your EC2 instance (Amazon Linux 2023, t3.medium or larger)
chmod +x deploy_ec2.sh
./deploy_ec2.sh
```

Make sure the EC2 security group allows inbound TCP on port 8501.

For production use, attach an IAM role to the instance instead of putting credentials in `.env`. The app detects this automatically — if no keys are in `.env`, boto3 falls back to the instance metadata service.

---

## Usage

1. **Upload documents**: Use the sidebar to upload PDFs or text files
2. **Wait for sync**: Bedrock indexes documents asynchronously. Check sync status in the sidebar — usually takes 1–3 minutes per document.
3. **Ask questions**: Type questions in the chat input. The app shows the answer, source citations, and retrieved chunks.

---

## Project structure

```
├── app.py              # Streamlit UI
├── bedrock_kb.py       # Retrieve + generate logic
├── s3_uploader.py      # File upload + KB sync trigger
├── config.py           # Config from env vars
├── setup_kb.py         # One-time KB creation script
├── deploy_ec2.sh       # EC2 setup script
├── requirements.txt
└── .env.example
```

---

## Known limitations

- **Sync is async**: After uploading, you have to wait for the ingestion job to finish. The app shows job status but doesn't auto-poll.
- **No auth**: The Streamlit app has no login — don't expose it on a public IP without adding authentication (Streamlit Community Cloud has auth built in, or put it behind a reverse proxy).
- **OpenSearch Serverless minimum cost**: Even at zero usage, AOSS has a minimum charge (~$700/month). For lower usage, consider using Bedrock KB with a Pinecone integration instead (pay-per-query).
- **Chunk quality**: Default fixed-size chunking (512 tokens, 20% overlap) works fine for most docs. For structured docs like financial tables or code, you'd want a custom chunking strategy.

---

## Tech stack

| Component | What | Why |
|---|---|---|
| Amazon Bedrock KB | Managed RAG | No vector DB ops, handles ingestion pipeline |
| Titan Embeddings v2 | Embedding model | Tight integration with Bedrock KB |
| Amazon Nova Lite | LLM | Amazon's own model — cost-effective, no third-party subscription needed |
| OpenSearch Serverless | Vector store | Bedrock-managed, no cluster maintenance |
| S3 | Document storage | Bedrock data source |
| Streamlit | UI | Fast to build, easy to deploy |
| EC2 | Hosting | Simple, straightforward for an internal tool |
