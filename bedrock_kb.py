"""
Bedrock Knowledge Base integration.

Uses the 2-step Retrieve + InvokeModel pattern instead of RetrieveAndGenerate.
Reason: RetrieveAndGenerate is a black box — no control over system prompt,
no way to inspect retrieved chunks, no reranking. For anything beyond a demo
you need this separation.
"""

import json
import boto3
import logging
from typing import Optional
from config import Config

logger = logging.getLogger(__name__)


def get_boto_session():
    """Build boto session — prefers env creds, falls back to IAM role on EC2."""
    kwargs = {"region_name": Config.AWS_REGION}

    if Config.AWS_ACCESS_KEY_ID and Config.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = Config.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = Config.AWS_SECRET_ACCESS_KEY

    return boto3.Session(**kwargs)


def retrieve_chunks(query: str, num_results: Optional[int] = None) -> list[dict]:
    """
    Retrieve relevant chunks from the Knowledge Base.

    Returns a list of dicts with 'content', 'score', and 'source' keys.
    Capping at MAX_RESULTS because more chunks past that point mostly add
    noise to the context window, not signal.
    """
    session = get_boto_session()
    client = session.client("bedrock-agent-runtime")

    n = num_results or Config.MAX_RESULTS

    try:
        response = client.retrieve(
            knowledgeBaseId=Config.KNOWLEDGE_BASE_ID,
            retrievalQuery={"text": query},
            retrievalConfiguration={
                "vectorSearchConfiguration": {
                    "numberOfResults": n,
                    "overrideSearchType": "HYBRID",  # semantic + keyword, not just cosine
                }
            },
        )
    except Exception as e:
        logger.error(f"Retrieve call failed: {e}")
        raise

    results = []
    for item in response.get("retrievalResults", []):
        source_uri = (
            item.get("location", {})
            .get("s3Location", {})
            .get("uri", "unknown source")
        )
        results.append({
            "content": item["content"]["text"],
            "score": round(item.get("score", 0.0), 4),
            "source": source_uri,
        })

    return results


def build_prompt(query: str, chunks: list[dict]) -> str:
    """
    Assemble the context + question into a prompt.

    Keeping it simple. The key thing is telling the model to say
    "I don't know" when the answer isn't in the chunks — otherwise
    it hallucinates confidently.
    """
    context_blocks = []
    for i, chunk in enumerate(chunks, 1):
        context_blocks.append(
            f"[Source {i}: {chunk['source']}]\n{chunk['content']}"
        )

    context = "\n\n---\n\n".join(context_blocks)

    return f"""You are an enterprise knowledge assistant. Answer the question using ONLY the provided context.

If the answer is not in the context, say: "I couldn't find relevant information in the knowledge base for this question."

Do not make up information. Cite the source number (e.g., [Source 1]) when referencing specific content.

Context:
{context}

Question: {query}

Answer:"""


def generate_answer(query: str, chunks: list[dict]) -> str:
    """
    Call the foundation model with the retrieved context to generate an answer.

    Using InvokeModel (not InvokeModelWithResponseStream) here for simplicity.
    Switch to streaming if you want tokens to appear progressively in the UI.
    """
    session = get_boto_session()
    client = session.client("bedrock-runtime")

    prompt = build_prompt(query, chunks)

    payload = {
        "anthropic_version": "bedrock-2023-05-31",
        "max_tokens": Config.MAX_TOKENS_RESPONSE,
        "messages": [
            {"role": "user", "content": prompt}
        ],
    }

    try:
        response = client.invoke_model(
            modelId=Config.BEDROCK_MODEL_ID,
            body=json.dumps(payload),
        )
        body = json.loads(response["body"].read())
        return body["content"][0]["text"]
    except Exception as e:
        logger.error(f"InvokeModel failed: {e}")
        raise


def ask(query: str) -> dict:
    """
    Main entry point: retrieve chunks, then generate answer.

    Returns dict with 'answer', 'sources', and 'chunks' so the
    UI can display citations and retrieved content separately.
    """
    chunks = retrieve_chunks(query)

    if not chunks:
        return {
            "answer": "No relevant documents found in the knowledge base.",
            "sources": [],
            "chunks": [],
        }

    answer = generate_answer(query, chunks)

    unique_sources = list(dict.fromkeys(c["source"] for c in chunks))

    return {
        "answer": answer,
        "sources": unique_sources,
        "chunks": chunks,
    }
