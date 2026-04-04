"""
One-time setup script: creates the S3 bucket, Bedrock Knowledge Base,
and wires the data source.

Run this once before the first deployment. After that, just use the app
to upload documents and trigger syncs.

Usage:
    python setup_kb.py

It prints the KB ID and data source ID at the end — put those in .env.
"""

import boto3
import json
import time
import sys
from config import Config


def get_clients():
    kwargs = {"region_name": Config.AWS_REGION}
    if Config.AWS_ACCESS_KEY_ID and Config.AWS_SECRET_ACCESS_KEY:
        kwargs["aws_access_key_id"] = Config.AWS_ACCESS_KEY_ID
        kwargs["aws_secret_access_key"] = Config.AWS_SECRET_ACCESS_KEY
    session = boto3.Session(**kwargs)
    return (
        session.client("s3"),
        session.client("bedrock-agent"),
        session.client("iam"),
    )


def create_s3_bucket(s3_client, bucket_name: str):
    print(f"Creating S3 bucket: {bucket_name}")
    try:
        if Config.AWS_REGION == "us-east-1":
            s3_client.create_bucket(Bucket=bucket_name)
        else:
            s3_client.create_bucket(
                Bucket=bucket_name,
                CreateBucketConfiguration={"LocationConstraint": Config.AWS_REGION},
            )
        # block public access — just in case
        s3_client.put_public_access_block(
            Bucket=bucket_name,
            PublicAccessBlockConfiguration={
                "BlockPublicAcls": True,
                "IgnorePublicAcls": True,
                "BlockPublicPolicy": True,
                "RestrictPublicBuckets": True,
            },
        )
        print(f"  Created: s3://{bucket_name}")
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        print(f"  Bucket already exists, continuing")
    except Exception as e:
        print(f"  Failed: {e}")
        sys.exit(1)


def get_or_create_bedrock_role(iam_client, bucket_name: str) -> str:
    """
    Create an IAM role for Bedrock to read from S3 and call Titan embeddings.

    This is the part that usually causes confusion. Bedrock needs its own
    role — it can't use your user credentials to ingest documents.
    """
    role_name = "BedrockKBRole"

    trust_policy = {
        "Version": "2012-10-17",
        "Statement": [{
            "Effect": "Allow",
            "Principal": {"Service": "bedrock.amazonaws.com"},
            "Action": "sts:AssumeRole",
        }]
    }

    inline_policy = {
        "Version": "2012-10-17",
        "Statement": [
            {
                "Effect": "Allow",
                "Action": ["s3:GetObject", "s3:ListBucket"],
                "Resource": [
                    f"arn:aws:s3:::{bucket_name}",
                    f"arn:aws:s3:::{bucket_name}/*",
                ],
            },
            {
                "Effect": "Allow",
                "Action": ["bedrock:InvokeModel"],
                "Resource": "arn:aws:bedrock:*::foundation-model/amazon.titan-embed-text-v2:0",
            },
        ]
    }

    try:
        response = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Role for Bedrock Knowledge Base to access S3",
        )
        role_arn = response["Role"]["Arn"]
        print(f"  Created IAM role: {role_arn}")
    except iam_client.exceptions.EntityAlreadyExistsException:
        role_arn = iam_client.get_role(RoleName=role_name)["Role"]["Arn"]
        print(f"  IAM role already exists: {role_arn}")

    iam_client.put_role_policy(
        RoleName=role_name,
        PolicyName="BedrockKBInlinePolicy",
        PolicyDocument=json.dumps(inline_policy),
    )

    # wait a moment for IAM propagation — skipping this causes intermittent failures
    print("  Waiting for IAM role to propagate...")
    time.sleep(15)

    return role_arn


def create_knowledge_base(bedrock_agent, role_arn: str) -> str:
    print("Creating Bedrock Knowledge Base...")

    response = bedrock_agent.create_knowledge_base(
        name="enterprise-kb",
        description="Enterprise document knowledge base",
        roleArn=role_arn,
        knowledgeBaseConfiguration={
            "type": "VECTOR",
            "vectorKnowledgeBaseConfiguration": {
                "embeddingModelArn": (
                    f"arn:aws:bedrock:{Config.AWS_REGION}::foundation-model/"
                    "amazon.titan-embed-text-v2:0"
                )
            },
        },
        storageConfiguration={
            "type": "OPENSEARCH_SERVERLESS",
            # Bedrock manages the AOSS collection automatically
            "opensearchServerlessConfiguration": {
                "collectionArn": "",  # Bedrock creates this if left empty
                "vectorIndexName": "enterprise-kb-index",
                "fieldMapping": {
                    "vectorField": "embedding",
                    "textField": "text",
                    "metadataField": "metadata",
                },
            },
        },
    )

    kb_id = response["knowledgeBase"]["knowledgeBaseId"]
    print(f"  Knowledge Base ID: {kb_id}")

    # wait for ACTIVE status
    print("  Waiting for KB to become ACTIVE...")
    for _ in range(20):
        status = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
        if status["knowledgeBase"]["status"] == "ACTIVE":
            print("  KB is ACTIVE")
            break
        time.sleep(10)
    else:
        print("  KB didn't become ACTIVE in time — check AWS console")
        sys.exit(1)

    return kb_id


def create_data_source(bedrock_agent, kb_id: str, bucket_name: str) -> str:
    print("Creating S3 data source...")

    response = bedrock_agent.create_data_source(
        knowledgeBaseId=kb_id,
        name="s3-docs",
        dataSourceConfiguration={
            "type": "S3",
            "s3Configuration": {
                "bucketArn": f"arn:aws:s3:::{bucket_name}",
                "inclusionPrefixes": ["knowledge-base-docs/"],
            },
        },
        vectorIngestionConfiguration={
            "chunkingConfiguration": {
                "chunkingStrategy": "FIXED_SIZE",
                "fixedSizeChunkingConfiguration": {
                    "maxTokens": 512,
                    "overlapPercentage": 20,  # overlap helps with answers that span chunk boundaries
                },
            }
        },
    )

    ds_id = response["dataSource"]["dataSourceId"]
    print(f"  Data Source ID: {ds_id}")
    return ds_id


def main():
    if not Config.S3_BUCKET_NAME:
        print("Error: S3_BUCKET_NAME not set in .env")
        sys.exit(1)

    s3_client, bedrock_agent, iam_client = get_clients()

    create_s3_bucket(s3_client, Config.S3_BUCKET_NAME)
    role_arn = get_or_create_bedrock_role(iam_client, Config.S3_BUCKET_NAME)
    kb_id = create_knowledge_base(bedrock_agent, role_arn)
    ds_id = create_data_source(bedrock_agent, kb_id, Config.S3_BUCKET_NAME)

    print("\n" + "="*50)
    print("Setup complete. Add these to your .env:")
    print(f"  KNOWLEDGE_BASE_ID={kb_id}")
    print(f"  DATA_SOURCE_ID={ds_id}")
    print("="*50)


if __name__ == "__main__":
    main()
