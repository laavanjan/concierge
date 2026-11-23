"""CLI: (re)build the policy index. Runs as a Kubernetes Job / compose one-shot in production.

    python -m rag.ingest            # build if the corpus fingerprint changed
    python -m rag.ingest --query "can I bring my dog?"
"""

from __future__ import annotations

import argparse
import json

from common.logging import setup_logging
from rag.retriever import build_retriever


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the Concierge policy index")
    parser.add_argument("--query", help="run a test query after building")
    args = parser.parse_args()
    setup_logging()
    retriever, stats = build_retriever()
    print(json.dumps(stats, indent=2))
    if args.query:
        result = retriever.search_sync(args.query)
        print(f"top_score={result.top_score:.3f} hits={len(result.chunks)}")
        for hit in result.chunks:
            print(f"  {hit.score:.3f}  {hit.chunk.chunk_id}  | {hit.chunk.text[:90]}")


if __name__ == "__main__":
    main()
