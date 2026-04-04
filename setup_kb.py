"""
One-time setup script: creates the AOSS collection, Bedrock Knowledge Base,
and wires the S3 data source.

Run this once before the first deployment. After that, just use the app
to upload documents and trigger syncs.

Usage:
    python setup_kb.py

Prints KB ID and data source ID at the end — put those in .env.
"""

import boto3
import json
import time
import sys
from config import Config


COLLECTION_NAME = "enterprise-kb-collection"


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
        session.client("opensearchserverless"),
        session.client("sts"),
    )


def get_account_id(sts_client) -> str:
    return sts_client.get_caller_identity()["Account"]


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


def get_or_create_bedrock_role(iam_client, bucket_name: str, account_id: str) -> str:
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
                "Resource": f"arn:aws:bedrock:{Config.AWS_REGION}::foundation-model/amazon.titan-embed-text-v2:0",
            },
            {
                "Effect": "Allow",
                "Action": ["aoss:APIAccessAll"],
                "Resource": f"arn:aws:aoss:{Config.AWS_REGION}:{account_id}:collection/*",
            },
        ]
    }

    try:
        response = iam_client.create_role(
            RoleName=role_name,
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Role for Bedrock Knowledge Base to access S3 and AOSS",
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
    print("  Updated IAM role policy")

    print("  Waiting for IAM role to propagate...")
    time.sleep(15)

    return role_arn


def create_aoss_collection(aoss_client, account_id: str, role_arn: str) -> str:
    """
    Create OpenSearch Serverless collection with required security policies.

    AOSS requires three policies before a collection can be created:
    encryption, network, and data access. Skipping any of them causes
    the collection creation to fail or the KB ingestion to silently error.
    """
    print(f"Setting up OpenSearch Serverless collection: {COLLECTION_NAME}")

    # encryption policy — required, AWS-managed key is fine for this
    enc_policy = json.dumps({
        "Rules": [{"Resource": [f"collection/{COLLECTION_NAME}"], "ResourceType": "collection"}],
        "AWSOwnedKey": True,
    })
    try:
        aoss_client.create_security_policy(
            name=f"{COLLECTION_NAME}-enc",
            type="encryption",
            policy=enc_policy,
        )
        print("  Created encryption policy")
    except aoss_client.exceptions.ConflictException:
        print("  Encryption policy already exists")

    # network policy — allow public access so Bedrock can reach it
    net_policy = json.dumps([{
        "Rules": [
            {"Resource": [f"collection/{COLLECTION_NAME}"], "ResourceType": "collection"},
            {"Resource": [f"collection/{COLLECTION_NAME}"], "ResourceType": "dashboard"},
        ],
        "AllowFromPublic": True,
    }])
    try:
        aoss_client.create_security_policy(
            name=f"{COLLECTION_NAME}-net",
            type="network",
            policy=net_policy,
        )
        print("  Created network policy")
    except aoss_client.exceptions.ConflictException:
        print("  Network policy already exists")

    # data access policy — Bedrock role + your account root need index permissions
    data_policy = json.dumps([{
        "Rules": [
            {
                "Resource": [f"index/{COLLECTION_NAME}/*"],
                "Permission": [
                    "aoss:CreateIndex", "aoss:DeleteIndex", "aoss:UpdateIndex",
                    "aoss:DescribeIndex", "aoss:ReadDocument", "aoss:WriteDocument",
                ],
                "ResourceType": "index",
            },
            {
                "Resource": [f"collection/{COLLECTION_NAME}"],
                "Permission": [
                    "aoss:CreateCollectionItems", "aoss:DeleteCollectionItems",
                    "aoss:UpdateCollectionItems", "aoss:DescribeCollectionItems",
                ],
                "ResourceType": "collection",
            },
        ],
        "Principal": [
            role_arn,
            f"arn:aws:iam::{account_id}:root",
        ],
    }])
    try:
        aoss_client.create_access_policy(
            name=f"{COLLECTION_NAME}-access",
            type="data",
            policy=data_policy,
        )
        print("  Created data access policy")
    except aoss_client.exceptions.ConflictException:
        print("  Data access policy already exists")

    # create the collection
    try:
        response = aoss_client.create_collection(
            name=COLLECTION_NAME,
            type="VECTORSEARCH",
        )
        collection_id = response["createCollectionDetail"]["id"]
        print(f"  Collection created: {collection_id}")
    except aoss_client.exceptions.ConflictException:
        response = aoss_client.list_collections(
            collectionFilters={"name": COLLECTION_NAME}
        )
        collection_id = response["collectionSummaries"][0]["id"]
        print(f"  Collection already exists: {collection_id}")

    collection_arn = f"arn:aws:aoss:{Config.AWS_REGION}:{account_id}:collection/{collection_id}"

    # wait for ACTIVE — usually takes 2-3 minutes
    print("  Waiting for collection to become ACTIVE (takes 2-3 mins)...")
    for _ in range(36):
        resp = aoss_client.list_collections(collectionFilters={"name": COLLECTION_NAME})
        status = resp["collectionSummaries"][0]["status"]
        if status == "ACTIVE":
            print(f"  Collection is ACTIVE")
            return collection_arn
        print(f"    status: {status}, waiting...")
        time.sleep(10)

    print("  Collection did not become ACTIVE in time — check AWS console")
    sys.exit(1)


def create_knowledge_base(bedrock_agent, role_arn: str, collection_arn: str) -> str:
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
            "opensearchServerlessConfiguration": {
                "collectionArn": collection_arn,
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

    print("  Waiting for KB to become ACTIVE...")
    for _ in range(20):
        status = bedrock_agent.get_knowledge_base(knowledgeBaseId=kb_id)
        if status["knowledgeBase"]["status"] == "ACTIVE":
            print("  KB is ACTIVE")
            return kb_id
        time.sleep(10)

    print("  KB did not become ACTIVE in time — check AWS console")
    sys.exit(1)


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
                    "overlapPercentage": 20,
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

    s3_client, bedrock_agent, iam_client, aoss_client, sts_client = get_clients()

    account_id = get_account_id(sts_client)
    print(f"AWS Account: {account_id} | Region: {Config.AWS_REGION}")

    create_s3_bucket(s3_client, Config.S3_BUCKET_NAME)
    role_arn = get_or_create_bedrock_role(iam_client, Config.S3_BUCKET_NAME, account_id)
    collection_arn = create_aoss_collection(aoss_client, account_id, role_arn)
    kb_id = create_knowledge_base(bedrock_agent, role_arn, collection_arn)
    ds_id = create_data_source(bedrock_agent, kb_id, Config.S3_BUCKET_NAME)

    print("\n" + "=" * 50)
    print("Setup complete. Add these to your .env:")
    print(f"  KNOWLEDGE_BASE_ID={kb_id}")
    print(f"  DATA_SOURCE_ID={ds_id}")
    print("=" * 50)


if __name__ == "__main__":
    main()
