"""AI Operations Evidence Layer - Phase 1 (design doc §4.11).

Pure instrumentation on top of the existing order pipeline: this module
never changes what any step does, what it returns, or whether it succeeds.
Every public function here is designed to fail silently (log locally and
move on) rather than ever raise into the real order flow - a metrics/
logging outage must never become a customer-facing outage. See
instrumented() below for the mechanics.

Event/run/day storage is intentionally decoupled from the order pipeline's
own Firestore writes - see AGENT_EVENTS_COLLECTION / ORDER_RUNS_COLLECTION /
DAILY_METRICS_COLLECTION in app/config.py.
"""
import contextlib
import contextvars
import datetime
import functools
import re
import time
import uuid
from concurrent.futures import ThreadPoolExecutor

from google.cloud import firestore

from app.config import (
    FIREBASE_PROJECT_ID, AGENT_EVENTS_COLLECTION, ORDER_RUNS_COLLECTION, DAILY_METRICS_COLLECTION,
)

_db = None

def _get_db():
    global _db
    if _db is None:
        _db = firestore.Client(project=FIREBASE_PROJECT_ID)
    return _db

# Every Firestore write this module makes goes through this pool rather than
# FastAPI's own BackgroundTasks - instrumented() wraps plain functions called
# from request handlers, BackgroundTasks callables, and the EIN/SCC pollers
# alike, and not all of those have a BackgroundTasks object in scope. Bounded
# so a Firestore outage queues writes instead of spawning unbounded threads;
# a write that never completes just means that one event/rollup is missing,
# never a blocked or broken order.
_executor = ThreadPoolExecutor(max_workers=4, thread_name_prefix="ai-ops-emit")

PROVIDER_VERTEX = "vertex_ai"
PROVIDER_NONE = "none"

# ---------------------------------------------------------------------------
# manual_baseline_min - PLACEHOLDER VALUES ONLY.
#
# These are rough, unsourced estimates of "how long would a solo founder
# spend on this step by hand" - written by Claude, not researched or
# documented per design-doc §8. They're good enough to make the Phase 1
# dashboard's "founder-hours saved" math run end to end, but every one of
# them needs a real, defensible source (a timed manual walkthrough, an
# industry benchmark, a quote from a service that does this step
# professionally, etc.) before this is used in anything external-facing
# like an XPRIZE submission. Flagging here rather than burying the caveat -
# whoever revisits this should not need to go hunting for it.
MANUAL_BASELINE_MIN = {
    "idea_intake": 5,
    "business_classification": 10,
    "name_generation": 20,
    "name_scc_check": 15,
    "document_assembly": 45,
    "brand_kit_generation": 90,
    "website_generation": 180,
    "marketing_plan_generation": 60,
    "ein_preparation": 20,
    "stripe_setup": 15,
    "human_review": 5,
    "state_filing": 30,
    "delivery": 10,
}

# Approximate Gemini pricing, USD per 1M tokens - also a placeholder (real
# pricing varies by exact model version/date and this isn't reconciled
# against actual GCP billing). For the dashboard's rough cost estimate only,
# never for anything billing-related.
_GEMINI_PRICE_PER_1M = {
    "gemini-2.5-flash": {"in": 0.30, "out": 2.50},
}
_DEFAULT_GEMINI_PRICE = {"in": 0.30, "out": 2.50}

def estimate_cost_usd(model: str | None, tokens_in: int, tokens_out: int) -> float:
    if not model or (not tokens_in and not tokens_out):
        return 0.0
    price = _GEMINI_PRICE_PER_1M.get(model, _DEFAULT_GEMINI_PRICE)
    return round((tokens_in / 1_000_000) * price["in"] + (tokens_out / 1_000_000) * price["out"], 6)

# ---------------------------------------------------------------------------
# PII redaction backstop - this is deliberately NOT the primary defense.
# The primary defense is that every call site in this codebase only ever
# passes short, template-built summaries (counts, category labels, status
# strings) into input_summary/output_summary, never raw generated text or
# raw form/order fields. This regex pass exists as a backstop in case a
# future call site accidentally does pass something PII-shaped through -
# per §4.6f/g and §4.11, this is non-negotiable, so it fails toward
# redaction (replace, truncate) rather than ever raising and blocking a
# real step.
_SSN_RE = re.compile(r"\b\d{3}-?\d{2}-?\d{4}\b")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_PHONE_RE = re.compile(r"\b(?:\+?1[-.\s]?)?\(?\d{3}\)?[-.\s]?\d{3}[-.\s]?\d{4}\b")
_MAX_SUMMARY_LEN = 256

def _scrub(text) -> str:
    if not text:
        return ""
    text = str(text)[:2000]
    text = _SSN_RE.sub("[redacted]", text)
    text = _EMAIL_RE.sub("[redacted]", text)
    text = _PHONE_RE.sub("[redacted]", text)
    return text[:_MAX_SUMMARY_LEN]

# ---------------------------------------------------------------------------
# Order/run context. Threaded via a contextvar rather than as an explicit
# parameter through every agent function - category_agent.py, name_agent.py,
# brand_agent.py, website_agent.py, marketing_agent.py, scc_name_check.py,
# and stripe_service.py all keep their existing signatures untouched.
# set_current_order is called once per order-scoped orchestration entry
# point (run_early_assets, run_document_generation, the wizard route
# handlers, etc.), right after that function already loads the order from
# Firestore - @instrumented then picks up order_id/run_id from here no
# matter how deep the actual call is.
_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ai_ops_ctx", default=None)
_seq_counters: dict[str, int] = {}

def new_run_id() -> str:
    return uuid.uuid4().hex

def ensure_run_id(order_ref, order: dict) -> str:
    """Returns order["run_id"], generating and persisting one first if this
    order predates the evidence layer (or a rare write race left it unset).
    Safe to call on every orchestration entry - a no-op read once it's set."""
    run_id = (order or {}).get("run_id")
    if run_id:
        return run_id
    run_id = new_run_id()
    try:
        order_ref.set({"run_id": run_id}, merge=True)
    except Exception as e:
        print(f"⚠️ ai_ops: could not persist run_id: {e}")
    return run_id

def set_current_order(order_id: str, run_id: str | None) -> None:
    _ctx.set({"order_id": order_id, "run_id": run_id})

def run_in_executor_with_context(loop, executor, fn, *args):
    """A drop-in replacement for loop.run_in_executor(executor, fn, *args)
    that preserves ai_ops's contextvar-based order/run context (and Gemini
    usage tracking) into the executor thread.

    asyncio.AbstractEventLoop.run_in_executor does NOT copy the calling
    context the way asyncio tasks/callbacks do (confirmed by hand) - it
    just submits fn directly to the executor, which runs in a plain thread
    with an empty context. Any route handler that calls set_current_order
    and then hands an @instrumented function to run_in_executor would
    silently lose that context (order_id/run_id would come back None) without
    this - use this helper instead wherever both matter together."""
    ctx = contextvars.copy_context()
    return loop.run_in_executor(executor, lambda: ctx.run(fn, *args))

def clear_current_order() -> None:
    _ctx.set(None)

def current_context() -> dict | None:
    return _ctx.get()

def _next_seq(run_id: str | None) -> int:
    """Best-effort display ordering, not authoritative - process-local, so
    it only reflects true order within a single Cloud Run instance/process.
    The dashboard's per-run timeline sorts by `timestamp`, the real source
    of truth; `seq` is a convenience tiebreaker for same-millisecond events."""
    if not run_id:
        return 0
    n = _seq_counters.get(run_id, 0) + 1
    _seq_counters[run_id] = n
    return n

# ---------------------------------------------------------------------------
# Gemini usage capture. app/agents/__init__.py's generate_content (the sole
# chokepoint every agent calls Gemini through) reports here after every real
# call; @instrumented reads the accumulated total back out when the wrapped
# step finishes. Best-effort only - if no instrumented step is in progress
# (bucket is None), this is a no-op, and any failure here is swallowed so it
# can never affect the real Gemini call it's reporting on.
_usage_ctx: contextvars.ContextVar[dict | None] = contextvars.ContextVar("ai_ops_usage_ctx", default=None)

def record_gemini_usage(model: str, usage_metadata) -> None:
    bucket = _usage_ctx.get()
    if bucket is None:
        return
    try:
        bucket["gemini_calls"] += 1
        bucket["tokens_in"] += int(getattr(usage_metadata, "prompt_token_count", 0) or 0)
        bucket["tokens_out"] += int(getattr(usage_metadata, "candidates_token_count", 0) or 0)
        bucket["model"] = model
    except Exception as e:
        print(f"⚠️ ai_ops: could not record Gemini usage: {e}")

# ---------------------------------------------------------------------------
# Event emission

def _write_event(event: dict) -> None:
    try:
        db = _get_db()
        db.collection(AGENT_EVENTS_COLLECTION).document(event["event_id"]).set(event)
        _update_rollups(db, event)
    except Exception as e:
        print(f"⚠️ ai_ops: failed to write agent event {event.get('event_id')}: {e}")

def _update_rollups(db, event: dict) -> None:
    """order_runs/{run_id} and daily_metrics/{date} incremental summaries -
    all counters use Firestore Increment so concurrent background writes
    for the same run/day never race a read-modify-write. Only terminal
    events (completed/failed) move counters - "started" events exist purely
    for the live activity feed, so they never double-count a step."""
    if event["phase"] not in ("completed", "failed"):
        return

    run_id = event.get("run_id")
    order_id = event.get("order_ref")
    is_success = event["status"] == "success"
    is_autonomous = event.get("autonomy") == "autonomous"
    gemini_calls_inc = 1 if event.get("model") else 0
    tokens_in = event.get("tokens_in") or 0
    tokens_out = event.get("tokens_out") or 0
    cost = event.get("cost_estimate_usd") or 0.0
    # Real human_duration_ms isn't measured yet in Phase 1 (see instrumented()
    # docstring) - manual_baseline_min for a non-autonomous step's success is
    # used as the stand-in "human minutes this order consumed" until real
    # timing is wired up, which is also exactly what "founder-hours saved"
    # wants for the autonomous side, just inverted.
    human_minutes = 0.0
    if event.get("human_duration_ms"):
        human_minutes = event["human_duration_ms"] / 60000.0
    elif is_success and not is_autonomous:
        human_minutes = (event.get("manual_baseline_min") or 0)
    # "Founder-hours saved" - manual_baseline_min for every autonomous step
    # that actually succeeded, i.e. what a solo founder would have spent by
    # hand on the thing the AI just did instead. Same placeholder caveat as
    # MANUAL_BASELINE_MIN itself.
    saved_minutes = (event.get("manual_baseline_min") or 0) if (is_success and is_autonomous) else 0.0

    day = event["timestamp"][:10] if isinstance(event.get("timestamp"), str) else time.strftime("%Y-%m-%d")

    common = {
        "steps_completed": firestore.Increment(1),
        "total_steps": firestore.Increment(1),
        "autonomous_steps": firestore.Increment(1 if (is_success and is_autonomous) else 0),
        "failed_steps": firestore.Increment(0 if is_success else 1),
        "gemini_calls": firestore.Increment(gemini_calls_inc),
        "tokens_in": firestore.Increment(tokens_in),
        "tokens_out": firestore.Increment(tokens_out),
        "cost_estimate_usd": firestore.Increment(cost),
        "human_touch_minutes": firestore.Increment(human_minutes),
        "founder_minutes_saved": firestore.Increment(saved_minutes),
    }

    if run_id:
        # No "status" string stored here on purpose - a naive overwrite-on-
        # every-event would let a later successful step silently erase an
        # earlier failure (this pipeline's steps aren't strictly sequential/
        # blocking, so both can genuinely happen for the same run). Readers
        # derive status instead: "error" if failed_steps > 0, else
        # "complete" if completed_at is set, else "in_progress" - both of
        # those only ever move one direction (failed_steps only grows,
        # completed_at is only ever set once, on a successful "delivery").
        run_fields = dict(common)
        run_fields.update({
            "run_id": run_id,
            "order_ref": order_id,
            "last_event_at": firestore.SERVER_TIMESTAMP,
            "last_step": event.get("step"),
            "last_status": event.get("status"),
        })
        if event.get("step") == "idea_intake" and is_success:
            run_fields["started_at"] = firestore.SERVER_TIMESTAMP
        if event.get("step") == "delivery" and is_success:
            run_fields["completed_at"] = firestore.SERVER_TIMESTAMP
        db.collection(ORDER_RUNS_COLLECTION).document(run_id).set(run_fields, merge=True)

    day_fields = dict(common)
    day_fields["date"] = day
    if event.get("step") == "idea_intake" and is_success:
        day_fields["orders_processed"] = firestore.Increment(1)
    db.collection(DAILY_METRICS_COLLECTION).document(day).set(day_fields, merge=True)

def emit_event(
    step: str,
    phase: str,
    *,
    step_label: str = "",
    model: str | None = None,
    provider: str = PROVIDER_NONE,
    operation: str = "prepare",
    autonomy: str = "autonomous",
    actor: str = "agent",
    status: str = "success",
    input_summary: str = "",
    output_summary: str = "",
    latency_ms: int | None = None,
    tokens_in: int = 0,
    tokens_out: int = 0,
    confidence: float | None = None,
    human_touched: bool = False,
    human_action: str | None = None,
    human_duration_ms: int | None = None,
    edit_delta: str | None = None,
    error_code: str | None = None,
    error_summary: str = "",
    trace_id: str | None = None,
) -> None:
    """Emits one AgentEvent, fire-and-forget (the Firestore write runs on a
    background thread pool - see _executor). Use directly for one-off
    checkpoints (human_review, state_filing, delivery, idea_intake) that
    don't correspond to a single wrapped function; see instrumented() for
    the decorator used on actual generation/check functions."""
    ctx = current_context() or {}
    order_id = ctx.get("order_id")
    run_id = ctx.get("run_id")
    cost = estimate_cost_usd(model, tokens_in, tokens_out)
    event = {
        "event_id": uuid.uuid4().hex,
        "run_id": run_id,
        "order_ref": order_id,
        "seq": _next_seq(run_id),
        # A plain ISO8601 UTC string rather than firestore.SERVER_TIMESTAMP -
        # _update_rollups (below) needs a real, immediately-readable value to
        # bucket into daily_metrics, and SERVER_TIMESTAMP is only a sentinel
        # until the write actually lands server-side. ISO8601 UTC strings
        # still sort correctly with Firestore's order_by, so nothing about
        # querying/ordering is lost.
        "timestamp": _now_iso(),
        "phase": phase,
        "actor": actor,
        "step": step,
        "step_label": step_label or step.replace("_", " ").title(),
        "model": model,
        "provider": provider,
        "operation": operation,
        "autonomy": autonomy,
        "input_summary": _scrub(input_summary),
        "output_summary": _scrub(output_summary),
        "status": status,
        "latency_ms": latency_ms,
        "tokens_in": tokens_in,
        "tokens_out": tokens_out,
        "cost_estimate_usd": cost,
        "confidence": confidence,
        "human_touched": human_touched,
        "human_action": human_action,
        "human_duration_ms": human_duration_ms,
        "edit_delta": edit_delta,
        "error_code": error_code,
        "error_summary": _scrub(error_summary),
        "trace_id": trace_id,
        "manual_baseline_min": MANUAL_BASELINE_MIN.get(step),
    }
    try:
        _executor.submit(_write_event, event)
    except Exception as e:
        print(f"⚠️ ai_ops: could not schedule agent event write: {e}")

def _now_iso() -> str:
    return datetime.datetime.now(datetime.timezone.utc).isoformat()

# ---------------------------------------------------------------------------
# Per-step output summarizers - deliberately explicit per step rather than a
# generic "stringify the result" fallback, so a new step type defaults to a
# safe, uninformative placeholder instead of accidentally leaking whatever
# shape its return value happens to have.
def _summarize(step: str, result) -> str:
    try:
        if step == "business_classification" and isinstance(result, dict):
            return f"category={result.get('category')}, confidence={result.get('confidence')}"
        if step == "name_generation" and isinstance(result, list):
            return f"{len(result)} name candidates generated"
        if step == "name_scc_check" and isinstance(result, dict):
            return f"status={result.get('status') or ('available' if result.get('available') else 'unavailable')}"
        if step == "document_assembly" and isinstance(result, (bytes, bytearray)):
            return f"LLC formation PDF assembled ({len(result)} bytes)"
        if step == "brand_kit_generation":
            return "brand kit generated: logo + palette + tagline"
        if step == "website_generation" and isinstance(result, dict):
            return f"website generated, template={result.get('template')}"
        if step == "marketing_plan_generation":
            return "marketing plan generated"
        if step == "stripe_setup":
            return "Stripe Connect account created"
    except Exception:
        pass
    return f"{step} completed"

# ---------------------------------------------------------------------------
# @instrumented - wraps a step function at its definition, so every call
# site is covered automatically without touching that call site.
def instrumented(
    step: str,
    *,
    model: str | None = None,
    provider: str = PROVIDER_NONE,
    operation: str = "generate",
    autonomy: str = "autonomous",
    actor: str = "agent",
    step_label: str = "",
    input_summary_fn=None,
):
    """input_summary_fn(*args, **kwargs) -> str, optional - builds
    input_summary from the wrapped function's own arguments (e.g. "business
    idea, ~140 chars") without ever passing the raw text through. Omit for
    steps where even that's not worth summarizing.

    Never changes the wrapped function's behavior: return value is passed
    through unchanged, and any exception is re-raised after the failure
    event is queued - the real order flow's own try/except handling
    (already written to survive any single agent call failing) is
    completely unaffected."""
    def decorator(fn):
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            ctx = current_context() or {}
            order_id = ctx.get("order_id")
            run_id = ctx.get("run_id")
            try:
                input_summary = input_summary_fn(*args, **kwargs) if input_summary_fn else ""
            except Exception:
                input_summary = ""

            emit_event(
                step, "started", step_label=step_label, model=model, provider=provider,
                operation=operation, autonomy=autonomy, actor=actor,
                input_summary=input_summary,
            )

            usage_token = _usage_ctx.set({"gemini_calls": 0, "tokens_in": 0, "tokens_out": 0, "model": None})
            t0 = time.monotonic()
            try:
                result = fn(*args, **kwargs)
            except Exception as e:
                usage = _usage_ctx.get()
                _usage_ctx.reset(usage_token)
                latency_ms = int((time.monotonic() - t0) * 1000)
                emit_event(
                    step, "failed", step_label=step_label, model=model, provider=provider,
                    operation=operation, autonomy=autonomy, actor=actor,
                    status="error", latency_ms=latency_ms,
                    tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
                    error_code=type(e).__name__, error_summary=str(e),
                )
                raise
            usage = _usage_ctx.get()
            _usage_ctx.reset(usage_token)
            latency_ms = int((time.monotonic() - t0) * 1000)
            emit_event(
                step, "completed", step_label=step_label, model=model, provider=provider,
                operation=operation, autonomy=autonomy, actor=actor,
                status="success", latency_ms=latency_ms,
                tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
                output_summary=_summarize(step, result),
            )
            return result
        return wrapper
    return decorator

@contextlib.contextmanager
def step(
    step_key: str,
    *,
    model: str | None = None,
    provider: str = PROVIDER_NONE,
    operation: str = "prepare",
    autonomy: str = "autonomous",
    actor: str = "agent",
    step_label: str = "",
    input_summary: str = "",
    output_summary: str = "",
    human_touched: bool = False,
    human_action: str | None = None,
):
    """Same started/completed/failed emission as @instrumented, but for a
    call site rather than a whole function definition - used where an
    orchestrator has its own skip-guard before the real work (run_scc_filing,
    run_ein_filing already return early for an order that's already filed),
    and decorating the whole function would log a misleading "step ran"
    event even when it didn't. Wrap only the real work:

        with ai_ops.step("state_filing", ...) as s:
            filed = file_llc_on_scc(...)
            s.output_summary = "LLC filed with Virginia SCC" if filed else "SCC filing did not complete"

    Also used directly for human_review/delivery checkpoints, which aren't
    single wrapped functions at all."""
    class _StepHandle:
        pass
    handle = _StepHandle()
    handle.output_summary = output_summary
    handle.error_summary = ""

    emit_event(
        step_key, "started", step_label=step_label, model=model, provider=provider,
        operation=operation, autonomy=autonomy, actor=actor,
        input_summary=input_summary, human_touched=human_touched, human_action=human_action,
    )
    usage_token = _usage_ctx.set({"gemini_calls": 0, "tokens_in": 0, "tokens_out": 0, "model": None})
    t0 = time.monotonic()
    try:
        yield handle
    except Exception as e:
        usage = _usage_ctx.get()
        _usage_ctx.reset(usage_token)
        latency_ms = int((time.monotonic() - t0) * 1000)
        emit_event(
            step_key, "failed", step_label=step_label, model=model, provider=provider,
            operation=operation, autonomy=autonomy, actor=actor,
            status="error", latency_ms=latency_ms,
            tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
            error_code=type(e).__name__, error_summary=handle.error_summary or str(e),
            human_touched=human_touched, human_action=human_action,
        )
        raise
    usage = _usage_ctx.get()
    _usage_ctx.reset(usage_token)
    latency_ms = int((time.monotonic() - t0) * 1000)
    emit_event(
        step_key, "completed", step_label=step_label, model=model, provider=provider,
        operation=operation, autonomy=autonomy, actor=actor,
        status="success", latency_ms=latency_ms,
        tokens_in=usage["tokens_in"], tokens_out=usage["tokens_out"],
        output_summary=handle.output_summary,
        human_touched=human_touched, human_action=human_action,
    )
