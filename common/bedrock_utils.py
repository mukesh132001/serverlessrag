"""Amazon Bedrock helpers: Titan embeddings and gpt-oss chat completions."""
import json
import logging
import os
from typing import Any, Dict, List, Tuple

import boto3

logger = logging.getLogger(__name__)

EMBEDDING_MODEL = os.getenv("EMBEDDING_MODEL", "amazon.titan-embed-text-v2:0")
EMBEDDING_DIMENSIONS = 1024
CHAT_MODEL = os.getenv("CHAT_MODEL", "openai.gpt-oss-20b-1:0")

_bedrock_runtime = None


def get_bedrock_client():
    """Create the Bedrock runtime client once per Lambda container."""
    global _bedrock_runtime
    if _bedrock_runtime is None:
        region = os.getenv("REGION") or os.getenv("AWS_REGION") or "us-east-1"
        _bedrock_runtime = boto3.client("bedrock-runtime", region_name=region)
    return _bedrock_runtime


def generate_embeddings(texts: List[str]) -> List[List[float]]:
    """Embed each text with Titan v2 (normalized, 1024 dimensions)."""
    client = get_bedrock_client()
    embeddings = []
    for text in texts:
        body = {"inputText": text, "dimensions": EMBEDDING_DIMENSIONS, "normalize": True}
        response = client.invoke_model(modelId=EMBEDDING_MODEL, body=json.dumps(body))
        result = json.loads(response["body"].read())
        embeddings.append(result["embedding"])
    return embeddings


def parse_chat_result(result: Dict[str, Any]) -> Tuple[str, Dict[str, int]]:
    """Pull the answer text and token usage out of an OpenAI-format response."""
    content = result["choices"][0]["message"]["content"]
    usage = result.get("usage") or {}
    return content, {
        "prompt_tokens": int(usage.get("prompt_tokens", 0)),
        "completion_tokens": int(usage.get("completion_tokens", 0)),
    }


def generate_chat_response(messages: List[Dict[str, str]]) -> Tuple[str, Dict[str, int]]:
    """Call the gpt-oss chat model on Bedrock. Returns (answer, token_usage)."""
    client = get_bedrock_client()
    body = {
        "messages": [{"role": m["role"], "content": m["content"]} for m in messages],
        "temperature": 0.2,
        "max_tokens": 2048,
    }
    response = client.invoke_model(modelId=CHAT_MODEL, body=json.dumps(body))
    return parse_chat_result(json.loads(response["body"].read()))
