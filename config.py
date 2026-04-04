import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    AWS_REGION = os.getenv("AWS_REGION", "us-east-1")
    AWS_ACCESS_KEY_ID = os.getenv("AWS_ACCESS_KEY_ID")
    AWS_SECRET_ACCESS_KEY = os.getenv("AWS_SECRET_ACCESS_KEY")

    KNOWLEDGE_BASE_ID = os.getenv("KNOWLEDGE_BASE_ID")
    DATA_SOURCE_ID = os.getenv("DATA_SOURCE_ID")
    S3_BUCKET_NAME = os.getenv("S3_BUCKET_NAME")

    # using 2-step retrieve+generate so we can control the prompt and inspect chunks
    BEDROCK_MODEL_ID = os.getenv(
        "BEDROCK_MODEL_ID",
        "anthropic.claude-3-5-sonnet-20241022-v2:0"
    )

    MAX_RESULTS = int(os.getenv("MAX_RESULTS", 5))

    # rough token limits to avoid runaway costs during demos
    MAX_TOKENS_RESPONSE = 1024

    @classmethod
    def validate(cls):
        missing = []
        required = ["KNOWLEDGE_BASE_ID", "S3_BUCKET_NAME"]
        for field in required:
            if not getattr(cls, field):
                missing.append(field)
        if missing:
            raise EnvironmentError(f"Missing required env vars: {', '.join(missing)}")
