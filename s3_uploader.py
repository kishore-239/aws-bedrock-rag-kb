"""
Upload documents to S3 and trigger Bedrock KB sync.

The sync after upload is async — Bedrock processes documents in the background.
Don't expect the document to be queryable immediately. For large files this
can take a few minutes.
"""

import boto3
import time
import logging
from pathlib import Path
from config import Config

logger = logging.getLogger(__name__)


def get_s3_client():
    session_kwargs = {"region_name": Config.AWS_REGION}
    if Config.AWS_ACCESS_KEY_ID and Config.AWS_SECRET_ACCESS_KEY:
        session_kwargs["aws_access_key_id"] = Config.AWS_ACCESS_KEY_ID
        session_kwargs["aws_secret_access_key"] = Config.AWS_SECRET_ACCESS_KEY
    session = boto3.Session(**session_kwargs)
    return session.client("s3"), session.client("bedrock-agent")


def upload_file_to_s3(file_bytes: bytes, filename: str) -> str:
    """Upload file to S3 and return the S3 URI."""
    s3_client, _ = get_s3_client()

    s3_key = f"knowledge-base-docs/{filename}"

    try:
        s3_client.put_object(
            Bucket=Config.S3_BUCKET_NAME,
            Key=s3_key,
            Body=file_bytes,
        )
        uri = f"s3://{Config.S3_BUCKET_NAME}/{s3_key}"
        logger.info(f"Uploaded {filename} -> {uri}")
        return uri
    except Exception as e:
        logger.error(f"S3 upload failed for {filename}: {e}")
        raise


def start_kb_sync() -> str:
    """
    Trigger ingestion job on the Knowledge Base data source.

    Returns the ingestion job ID. The job runs async — check status
    separately if you need to wait for it.
    """
    if not Config.DATA_SOURCE_ID:
        logger.warning("DATA_SOURCE_ID not set, skipping KB sync trigger")
        return ""

    _, bedrock_agent = get_s3_client()

    try:
        response = bedrock_agent.start_ingestion_job(
            knowledgeBaseId=Config.KNOWLEDGE_BASE_ID,
            dataSourceId=Config.DATA_SOURCE_ID,
        )
        job_id = response["ingestionJob"]["ingestionJobId"]
        logger.info(f"KB sync started: job_id={job_id}")
        return job_id
    except Exception as e:
        logger.error(f"Failed to start KB sync: {e}")
        raise


def get_sync_status(job_id: str) -> dict:
    """Check status of an ingestion job."""
    if not job_id or not Config.DATA_SOURCE_ID:
        return {"status": "UNKNOWN"}

    _, bedrock_agent = get_s3_client()

    try:
        response = bedrock_agent.get_ingestion_job(
            knowledgeBaseId=Config.KNOWLEDGE_BASE_ID,
            dataSourceId=Config.DATA_SOURCE_ID,
            ingestionJobId=job_id,
        )
        job = response["ingestionJob"]
        return {
            "status": job["status"],
            "statistics": job.get("statistics", {}),
        }
    except Exception as e:
        logger.error(f"Failed to get sync status: {e}")
        return {"status": "ERROR", "error": str(e)}


def list_kb_documents() -> list[dict]:
    """List files currently in the S3 knowledge base folder."""
    s3_client, _ = get_s3_client()

    try:
        response = s3_client.list_objects_v2(
            Bucket=Config.S3_BUCKET_NAME,
            Prefix="knowledge-base-docs/",
        )
        docs = []
        for obj in response.get("Contents", []):
            docs.append({
                "filename": obj["Key"].split("/")[-1],
                "size_kb": round(obj["Size"] / 1024, 1),
                "last_modified": obj["LastModified"].strftime("%Y-%m-%d %H:%M"),
            })
        return docs
    except Exception as e:
        logger.error(f"Failed to list KB documents: {e}")
        return []
