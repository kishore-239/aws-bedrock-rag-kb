"""
Setup script for Enterprise KB Q&A.

Handles S3 bucket and IAM role creation programmatically.
The Knowledge Base itself must be created via AWS Console — OpenSearch Serverless
requires a separate account subscription that the console activates automatically
but the API does not.

Usage:
    Step 1: python setup_kb.py
    Step 2: Follow the printed console instructions to create the KB
    Step 3: Add KNOWLEDGE_BASE_ID and DATA_SOURCE_ID to .env
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
        session.client("iam"),
        session.client("sts"),
    )


def get_account_id(sts_client) -> str:
    return sts_client.get_caller_identity()["Account"]


def create_s3_bucket(s3_client, bucket_name: str):
    print(f"\n[1/2] Creating S3 bucket: {bucket_name}")
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
        print(f"  Done: s3://{bucket_name}")
    except s3_client.exceptions.BucketAlreadyOwnedByYou:
        print(f"  Already exists, continuing")
    except Exception as e:
        print(f"  Failed: {e}")
        sys.exit(1)


def get_or_create_bedrock_role(iam_client, bucket_name: str, account_id: str) -> str:
    print(f"\n[2/2] Setting up IAM role: BedrockKBRole")

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
            RoleName="BedrockKBRole",
            AssumeRolePolicyDocument=json.dumps(trust_policy),
            Description="Allows Bedrock Knowledge Base to access S3 and OpenSearch Serverless",
        )
        role_arn = response["Role"]["Arn"]
        print(f"  Created: {role_arn}")
    except iam_client.exceptions.EntityAlreadyExistsException:
        role_arn = iam_client.get_role(RoleName="BedrockKBRole")["Role"]["Arn"]
        print(f"  Already exists: {role_arn}")

    iam_client.put_role_policy(
        RoleName="BedrockKBRole",
        PolicyName="BedrockKBInlinePolicy",
        PolicyDocument=json.dumps(inline_policy),
    )
    print(f"  Permissions updated")
    return role_arn


def print_console_instructions(bucket_name: str, role_arn: str):
    print("\n" + "=" * 60)
    print("NEXT STEP: Create the Knowledge Base in AWS Console")
    print("=" * 60)
    print("""
OpenSearch Serverless requires console activation — do this once:

1. Go to: AWS Console → Amazon Bedrock → Knowledge bases → Create

2. Knowledge Base settings:
   - Name: enterprise-kb
   - IAM role: select existing → BedrockKBRole

3. Data source:
   - Type: Amazon S3
   - S3 URI: s3://""" + bucket_name + """/knowledge-base-docs/
   - Chunking: Fixed size, 512 tokens, 20% overlap

4. Embeddings model:
   - Select: Titan Text Embeddings V2

5. Vector store:
   - Select: Create a new vector store (Quick create)
   - This auto-creates the OpenSearch Serverless collection

6. Click Create Knowledge Base and wait for it to show ACTIVE

7. Open the Knowledge Base → copy the Knowledge Base ID
   Open the Data source → copy the Data Source ID

8. Add both to your .env file:
   KNOWLEDGE_BASE_ID=<paste here>
   DATA_SOURCE_ID=<paste here>

Then run: streamlit run app.py
""")
    print("=" * 60)


def main():
    if not Config.S3_BUCKET_NAME:
        print("Error: S3_BUCKET_NAME not set in .env")
        sys.exit(1)

    s3_client, iam_client, sts_client = get_clients()

    account_id = get_account_id(sts_client)
    print(f"Account: {account_id} | Region: {Config.AWS_REGION}")

    create_s3_bucket(s3_client, Config.S3_BUCKET_NAME)
    role_arn = get_or_create_bedrock_role(iam_client, Config.S3_BUCKET_NAME, account_id)

    print_console_instructions(Config.S3_BUCKET_NAME, role_arn)


if __name__ == "__main__":
    main()
