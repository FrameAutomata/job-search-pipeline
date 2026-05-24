"""Synchronous parallel batch evaluator supporting multiple LLM providers.

Processes jobs from batch-input.tsv immediately (no async wait) using parallel
workers. Results are written as they complete.

Supported providers
-------------------
  anthropic   claude-sonnet-4-6 (default)      ANTHROPIC_API_KEY
  gemini      gemini-2.0-flash  (default)       GEMINI_API_KEY
  openai      gpt-4o-mini       (default)       OPENAI_API_KEY
  groq        llama-3.3-70b-versatile (default) GROQ_API_KEY
  ollama      qwen2.5:32b       (default)       OLLAMA_BASE_URL (default: http://localhost:11434)

Provider auto-detection: BATCH_PROVIDER env var, then first key found in the order above.

Requirements (install only the provider you need):
  pip install anthropic                  # anthropic
  pip install google-generativeai        # gemini
  pip install openai                     # openai / groq / ollama
"""

import json
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

from pipeline._batch_common import (
    build_system_prompt,
    build_user_message,
    load_pending,
    load_state,
    max_report_num,
    max_tracker_num,
    read_text,
    run_merge_tracker,
    write_job_result,
)

ROOT = Path(__file__).resolve().parent.parent

PROVIDER_DEFAULTS: dict[str, str] = {
    "anthropic": "claude-sonnet-4-6",
    "gemini": "gemini-2.0-flash",
    "openai": "gpt-4o-mini",
    "groq": "llama-3.3-70b-versatile",
    "ollama": "qwen2.5:32b",
}


# ── Provider dispatch ────────────────────────────────────────────────────────

def _call_anthropic(system: str, user: str, model: str) -> str:
    import anthropic as _a
    client = _a.Anthropic(api_key=os.environ["ANTHROPIC_API_KEY"])
    msg = client.messages.create(
        model=model,
        max_tokens=8192,
        system=system,
        messages=[{"role": "user", "content": user}],
    )
    return msg.content[0].text


def _call_gemini(system: str, user: str, model: str) -> str:
    import google.generativeai as genai  # type: ignore[import]
    genai.configure(api_key=os.environ["GEMINI_API_KEY"])
    m = genai.GenerativeModel(model_name=model, system_instruction=system)
    resp = m.generate_content(user)
    return resp.text


def _call_openai_compat(system: str, user: str, model: str, api_key: str, base_url: str | None = None) -> str:
    from openai import OpenAI  # type: ignore[import]
    client = OpenAI(api_key=api_key, base_url=base_url)
    resp = client.chat.completions.create(
        model=model,
        max_tokens=8192,
        messages=[
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    )
    return resp.choices[0].message.content


def _call(provider: str, system: str, user: str, model: str) -> str:
    if provider == "anthropic":
        return _call_anthropic(system, user, model)
    if provider == "gemini":
        return _call_gemini(system, user, model)
    if provider == "openai":
        return _call_openai_compat(system, user, model, api_key=os.environ["OPENAI_API_KEY"])
    if provider == "groq":
        return _call_openai_compat(
            system, user, model,
            api_key=os.environ["GROQ_API_KEY"],
            base_url="https://api.groq.com/openai/v1",
        )
    if provider == "ollama":
        base = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434").rstrip("/") + "/v1"
        return _call_openai_compat(system, user, model, api_key="ollama", base_url=base)
    raise ValueError(f"Unknown provider: {provider!r}. Choose: {', '.join(PROVIDER_DEFAULTS)}")


# ── Provider validation ──────────────────────────────────────────────────────

# Detection order matters: free-tier providers checked first.
# ollama has no required key, so it is excluded from auto-detection.
_PROVIDER_KEYS: dict[str, str] = {
    "gemini": "GEMINI_API_KEY",
    "groq": "GROQ_API_KEY",
    "openai": "OPENAI_API_KEY",
    "anthropic": "ANTHROPIC_API_KEY",
}


def _detect_provider() -> str | None:
    explicit = os.environ.get("BATCH_PROVIDER", "").strip().lower()
    if explicit:
        return explicit
    for provider, key in _PROVIDER_KEYS.items():
        if os.environ.get(key):
            return provider
    return None


def _check_provider(provider: str) -> str | None:
    key = _PROVIDER_KEYS.get(provider)
    if key and not os.environ.get(key):
        return f"{key} not set"
    return None


# ── Worker ───────────────────────────────────────────────────────────────────

def _process_one(
    job_meta: dict,
    system_prompt: str,
    provider: str,
    model: str,
    reports_dir: Path,
    tracker_dir: Path,
    today: str,
    state: dict,
    state_lock: threading.Lock,
) -> tuple[bool, str, str | None]:
    """Evaluate one job. Returns (success, job_id, error_or_None)."""
    jid = job_meta["id"]
    try:
        response = _call(provider, system_prompt, build_user_message(job_meta, today), model)
        out = write_job_result(response, job_meta, reports_dir, tracker_dir, today)

        with state_lock:
            state["jobs"][jid]["status"] = "completed"
            state["jobs"][jid]["report"] = f"reports/{out['report_file']}" if out["report_file"] else None
            if out["summary"].get("score") is not None:
                state["jobs"][jid]["score"] = out["summary"]["score"]

        return True, jid, None

    except Exception as exc:
        with state_lock:
            state["jobs"][jid]["status"] = "failed"
            state["jobs"][jid]["error"] = str(exc)
        return False, jid, str(exc)


# ── Main entry point ─────────────────────────────────────────────────────────

def run(
    career_ops: Path,
    provider: str | None = None,
    model: str | None = None,
    concurrency: int = 3,
    dry_run: bool = False,
) -> int:
    """Evaluate pending jobs synchronously. Returns number of jobs processed."""
    provider = (provider or _detect_provider() or "").strip().lower()
    if not provider:
        print(
            "error: no LLM provider configured. Set BATCH_PROVIDER or one of: "
            "GEMINI_API_KEY, GROQ_API_KEY, OPENAI_API_KEY, ANTHROPIC_API_KEY, "
            "or OLLAMA_BASE_URL with BATCH_PROVIDER=ollama",
            file=sys.stderr,
        )
        return 0

    if provider not in PROVIDER_DEFAULTS:
        print(f"error: unknown provider {provider!r}. Choose: {', '.join(PROVIDER_DEFAULTS)}", file=sys.stderr)
        return 0

    model = model or os.environ.get("BATCH_MODEL", PROVIDER_DEFAULTS[provider])
    today = datetime.now().strftime("%Y-%m-%d")

    batch_input = career_ops / "batch" / "batch-input.tsv"
    state_path = career_ops / "batch" / "batch-api-state.json"
    reports_dir = career_ops / "reports"
    tracker_dir = career_ops / "batch" / "tracker-additions"
    applications_md = career_ops / "data" / "applications.md"

    if not batch_input.exists():
        print("[batch-eval] no batch-input.tsv found — nothing to evaluate")
        return 0

    state = load_state(state_path)
    pending = load_pending(batch_input, state)

    if not pending:
        print("[batch-eval] all jobs already evaluated — nothing to do")
        return 0

    print(f"[batch-eval] {len(pending)} job(s) | provider={provider} | model={model} | workers={concurrency}")

    if dry_run:
        for row in pending[:5]:
            print(f"  [{row['id']}] {row.get('source') or '?'} / {row.get('notes') or '?'}")
        if len(pending) > 5:
            print(f"  ... and {len(pending) - 5} more")
        return len(pending)

    err = _check_provider(provider)
    if err:
        print(f"error: {err}", file=sys.stderr)
        return 0

    cv = read_text(career_ops / "cv.md")
    if not cv:
        print("error: career-ops/cv.md not found — cannot evaluate without a CV", file=sys.stderr)
        return 0

    system_prompt = build_system_prompt(
        cv,
        read_text(career_ops / "config" / "profile.yml"),
        read_text(career_ops / "modes" / "_profile.md"),
        read_text(career_ops / "article-digest.md"),
    )

    report_counter = max_report_num(reports_dir, state)
    tracker_counter = max_tracker_num(applications_md, state)

    reports_dir.mkdir(parents=True, exist_ok=True)
    tracker_dir.mkdir(parents=True, exist_ok=True)

    # Pre-assign numbers and load JDs before spawning threads
    jobs: list[dict] = []
    for row in pending:
        jid = str(row["id"]).strip()
        company = (row.get("source") or "").strip()
        role = (row.get("notes") or "").strip()
        report_counter += 1
        tracker_counter += 1
        meta = {
            "id": jid,
            "url": (row.get("url") or "").strip(),
            "company": company,
            "role": role,
            "report_num": f"{report_counter:03d}",
            "tracker_num": tracker_counter,
            "jd_text": read_text(career_ops / "batch" / "jds" / f"{jid}.txt"),
            "status": "pending",
        }
        jobs.append(meta)
        state_entry = dict(meta)
        state_entry.pop("jd_text", None)
        state["jobs"][jid] = state_entry

    state["provider"] = provider
    state["model"] = model
    state["status"] = "in_progress"
    state_path.parent.mkdir(parents=True, exist_ok=True)

    state_lock = threading.Lock()
    processed = failed = 0

    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        futures = {
            pool.submit(
                _process_one,
                meta, system_prompt, provider, model,
                reports_dir, tracker_dir, today,
                state, state_lock,
            ): meta
            for meta in jobs
        }
        for future in as_completed(futures):
            meta = futures[future]
            success, jid, err_msg = future.result()
            if success:
                report = state["jobs"][jid].get("report", "")
                score = state["jobs"][jid].get("score", "?")
                print(f"  [{jid}] {meta['company'] or '?'} -> score={score} {report or '(no report)'}")
                processed += 1
            else:
                print(f"  [{jid}] FAILED: {err_msg}")
                failed += 1

            # Persist state after each completion — snapshot under lock, write outside
            with state_lock:
                snapshot = json.dumps(state, indent=2, ensure_ascii=False)
            state_path.write_text(snapshot, encoding="utf-8")

    state["status"] = "completed"
    state["completed_at"] = datetime.utcnow().isoformat() + "Z"
    state_path.write_text(json.dumps(state, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"[batch-eval] done — processed={processed} failed={failed}")

    if processed > 0:
        run_merge_tracker(career_ops)

    return processed


if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv(ROOT / ".env")
    career_ops_path = Path(os.environ.get("CAREER_OPS_PATH", ROOT / "career-ops")).resolve()
    provider_arg = None
    model_arg = None
    concurrency_arg = 3
    dry = "--dry-run" in sys.argv
    for i, a in enumerate(sys.argv[1:], 1):
        if a == "--provider" and i + 1 < len(sys.argv):
            provider_arg = sys.argv[i + 1]
        elif a == "--model" and i + 1 < len(sys.argv):
            model_arg = sys.argv[i + 1]
        elif a == "--concurrency" and i + 1 < len(sys.argv):
            concurrency_arg = int(sys.argv[i + 1])
    sys.exit(0 if run(career_ops_path, provider_arg, model_arg, concurrency_arg, dry) >= 0 else 1)
