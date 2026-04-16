from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from .langgraph_onboarding import build_langgraph_onboarding_payload


DEFAULT_GEMINI_MODEL = "gemini-2.0-flash"
DEFAULT_GEMINI_BASE_URL = "https://generativelanguage.googleapis.com/v1beta/openai"
DEFAULT_GEMINI_KEY_ENV = "GEMINI_API_KEY"


def build_gemini_onboarding_payload(
    *,
    run_dir: str,
    knowledge_db: str = ".data/knowledge.db",
    top_n: int = 10,
    llm_enable: bool = True,
    llm_model: str = DEFAULT_GEMINI_MODEL,
    llm_base_url: str = DEFAULT_GEMINI_BASE_URL,
    llm_api_key_env: str = DEFAULT_GEMINI_KEY_ENV,
    llm_timeout_seconds: float = 60.0,
    dotenv_path: str | None = ".env",
) -> dict[str, Any]:
    """Gemini-focused wrapper around LangGraph onboarding payload generation.

    This keeps the existing OpenAI-compatible flow for OpenAI/Groq unchanged while
    providing Gemini-friendly defaults in a dedicated command.
    """

    payload = build_langgraph_onboarding_payload(
        run_dir=str(run_dir),
        knowledge_db=str(knowledge_db),
        top_n=max(int(top_n), 1),
        llm_enable=bool(llm_enable),
        llm_model=str(llm_model or DEFAULT_GEMINI_MODEL),
        llm_base_url=str(llm_base_url or DEFAULT_GEMINI_BASE_URL),
        llm_api_key_env=str(llm_api_key_env or DEFAULT_GEMINI_KEY_ENV),
        llm_timeout_seconds=max(float(llm_timeout_seconds or 60.0), 1.0),
        dotenv_path=str(dotenv_path or ".env"),
    )

    # Include wrapper metadata so outputs clearly show the Gemini intent.
    payload["provider"] = "gemini"
    payload["providerDefaults"] = {
        "model": DEFAULT_GEMINI_MODEL,
        "base_url": DEFAULT_GEMINI_BASE_URL,
        "api_key_env": DEFAULT_GEMINI_KEY_ENV,
    }
    return payload


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate onboarding proposal payload with Gemini-focused defaults. "
            "Uses LangGraph onboarding under the hood and OpenAI-compatible API format."
        )
    )
    parser.add_argument(
        "--run-dir",
        required=True,
        help="Path to a single run directory under .data/runs/<site>/<unit_id>",
    )
    parser.add_argument(
        "--knowledge-db",
        default=".data/knowledge.db",
        help="Path to sqlite knowledge database",
    )
    parser.add_argument(
        "--top-n",
        type=int,
        default=10,
        help="Number of top endpoints to include",
    )
    parser.add_argument(
        "--llm-disable",
        action="store_true",
        help="Disable LLM enrichment (deterministic proposal only)",
    )
    parser.add_argument(
        "--llm-model",
        default=DEFAULT_GEMINI_MODEL,
        help="Gemini model name for LLM enrichment",
    )
    parser.add_argument(
        "--llm-base-url",
        default=DEFAULT_GEMINI_BASE_URL,
        help="Gemini OpenAI-compatible base URL",
    )
    parser.add_argument(
        "--llm-api-key-env",
        default=DEFAULT_GEMINI_KEY_ENV,
        help="Environment variable that stores Gemini API key",
    )
    parser.add_argument(
        "--llm-timeout-seconds",
        type=float,
        default=60.0,
        help="LLM request timeout in seconds",
    )
    parser.add_argument(
        "--dotenv-path",
        default=".env",
        help="Optional dotenv path to preload API keys before reading env vars",
    )
    parser.add_argument(
        "--out",
        help="Optional path to write payload JSON",
    )
    parser.add_argument(
        "--patch-plan-out",
        help="Optional path to write patch-plan JSON artifact",
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    payload = build_gemini_onboarding_payload(
        run_dir=str(args.run_dir),
        knowledge_db=str(args.knowledge_db),
        top_n=int(args.top_n),
        llm_enable=not bool(args.llm_disable),
        llm_model=str(args.llm_model),
        llm_base_url=str(args.llm_base_url),
        llm_api_key_env=str(args.llm_api_key_env),
        llm_timeout_seconds=float(args.llm_timeout_seconds),
        dotenv_path=str(args.dotenv_path) if args.dotenv_path else None,
    )

    rendered = json.dumps(payload, indent=2, ensure_ascii=True)

    if args.out:
        Path(args.out).write_text(rendered + "\n", encoding="utf-8")

    if args.patch_plan_out:
        patch_payload = json.dumps(dict(payload.get("patchPlan") or {}), indent=2, ensure_ascii=True)
        Path(args.patch_plan_out).write_text(patch_payload + "\n", encoding="utf-8")

    print(rendered)


if __name__ == "__main__":
    main()
