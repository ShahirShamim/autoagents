#!/usr/bin/env python
"""Idempotent setup for the autoagents RAG Engine corpus.

New GCP projects can't use Spanner-mode RAG Engine in us-central1, so we set the
region's RAG Engine to Serverless (Basic) tier first, then create the corpus.

Run: uv run python scripts/setup_rag_corpus.py
Prints the corpus resource name (set it as RAG_CORPUS in .env).
"""
import os

import vertexai
from vertexai import rag
from vertexai.rag.utils import resources as r

PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "autoagents-500500")
LOCATION = "us-central1"
DISPLAY_NAME = "autoagents-docs"


def main() -> None:
    vertexai.init(project=PROJECT, location=LOCATION)

    # 1. Ensure the region's RAG Engine managed DB is in Serverless (Basic) tier.
    cfg_name = f"projects/{PROJECT}/locations/{LOCATION}/ragEngineConfig"
    try:
        rag.update_rag_engine_config(
            rag_engine_config=rag.RagEngineConfig(
                name=cfg_name,
                rag_managed_db_config=rag.RagManagedDbConfig(tier=r.Basic()),
            )
        )
        print("RAG Engine config set to Serverless (Basic).")
    except Exception as exc:  # noqa: BLE001 - already-basic / race is fine
        print(f"(engine config update note: {exc})")

    # 2. Create the corpus if it doesn't already exist.
    existing = None
    for c in rag.list_corpora():
        if c.display_name == DISPLAY_NAME:
            existing = c
            break
    corpus = existing or rag.create_corpus(display_name=DISPLAY_NAME)
    print(f"CORPUS {corpus.name}")


if __name__ == "__main__":
    main()
