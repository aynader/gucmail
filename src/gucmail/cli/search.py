"""The local index, OCR, semantic search, clusters, insights and cleanup."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import typer

from gucmail.cli import core
from gucmail.errors import (
    ConfigurationError,
    ConflictError,
)
from gucmail.security import sanitize_terminal
from gucmail.services.attachment_indexing import MailboxIndexProducer, attachment_index_preferences
from gucmail.services.cleanup import CleanupPlanner
from gucmail.services.clusters import cluster_messages
from gucmail.services.index import SearchIndex
from gucmail.services.insights import (
    analyze_mailbox,
    duplicate_insights,
    sender_insights,
    storage_insights,
)
from gucmail.services.message_scan import scan_message_summaries
from gucmail.services.ocr import OcrService
from gucmail.services.semantic import SemanticStore, account_identity
from gucmail.services.semantic_search import SemanticSearchService

index_app = typer.Typer(help="The local search index that makes search fast.")


ocr_app = typer.Typer(help="(Advanced) Read text from images in attachments.")


semantic_app = typer.Typer(help="(Advanced) Search by meaning, on this computer only.")


clusters_app = typer.Typer(help="Group similar mail together.")


insights_app = typer.Typer(help="What takes up space and who sends the most.")


cleanup_app = typer.Typer(help="Free up space safely, with a preview first.")


@index_app.command("status")
def index_status(ctx: typer.Context) -> None:
    """Show local FTS document count, size, and last update."""

    core.run(
        ctx,
        "index status",
        lambda: SearchIndex(core.cli_state(ctx).context.paths.index_database).stats(),
        title="Search index",
        source="search_index",
    )


@index_app.command("update")
@index_app.command("build")
def index_build(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
    include_bodies: bool = typer.Option(False, help="Fetch and index safe text bodies."),
) -> None:
    """Build or refresh a bounded local index page from Exchange."""

    def action() -> dict[str, Any]:
        preferences = attachment_index_preferences(core.settings_service(ctx))
        if preferences.enabled:
            core.confirm(
                ctx,
                f"Index one bounded page from {folder!r} (limit {limit}). " + preferences.review(),
                f"index-attachments:{folder}:{limit}:{include_bodies}:{preferences.fingerprint}",
            )
        if attachment_index_preferences(core.settings_service(ctx)) != preferences:
            raise ConflictError(
                "Attachment indexing settings changed after review; retry with the current policy."
            )
        gateway = core.gateway(ctx)
        producer = MailboxIndexProducer(gateway, preferences)
        index = SearchIndex(core.cli_state(ctx).context.paths.index_database)
        count = index.upsert_many(
            producer.documents(folder=folder, limit=limit, include_bodies=include_bodies)
        )
        return {
            "indexed": count,
            "attachment_processing": producer.report.summary(),
            "folder": folder,
            "bodies": include_bodies,
            "stats": index.stats(),
        }

    core.run(ctx, "index build", action, title="Index build", source="search_index")


@index_app.command("rebuild")
def index_rebuild(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
    include_bodies: bool = typer.Option(False),
) -> None:
    """Replace the disposable local index with one bounded mailbox view."""

    def action() -> dict[str, Any]:
        index = SearchIndex(core.cli_state(ctx).context.paths.index_database)
        revision = index.revision()
        preferences = attachment_index_preferences(core.settings_service(ctx))
        scope = hashlib.sha256(
            json.dumps(
                [
                    folder,
                    limit,
                    include_bodies,
                    preferences.fingerprint,
                    revision.documents,
                    revision.updated_at,
                    revision.revision_id,
                ],
                ensure_ascii=True,
            ).encode("utf-8")
        ).hexdigest()
        core.confirm(
            ctx,
            f"Replace {revision.documents} disposable index rows from "
            f"{sanitize_terminal(folder, max_length=200)!r}, at most {limit} messages, "
            f"bodies={include_bodies}, up to 32 MiB prepared data? "
            "The prior index remains until the atomic commit. " + preferences.review(),
            "index-rebuild:v2:" + scope,
        )
        if attachment_index_preferences(core.settings_service(ctx)) != preferences:
            raise ConflictError(
                "Attachment indexing settings changed after review; retry with the current policy."
            )
        gateway = core.gateway(ctx)
        producer = MailboxIndexProducer(gateway, preferences)
        replaced = index.replace_many(
            producer.documents(folder=folder, limit=limit, include_bodies=include_bodies),
            limit=limit,
            expected_revision=revision,
        )
        return {
            "removed": replaced.removed,
            "indexed": replaced.indexed,
            "attachment_processing": producer.report.summary(),
            "stats": index.stats(),
        }

    core.run(ctx, "index rebuild", action, title="Index rebuild", source="search_index")


@index_app.command("verify")
def index_verify(ctx: typer.Context) -> None:
    """Run SQLite integrity, schema, and FTS row-count checks."""

    core.run(
        ctx,
        "index verify",
        lambda: SearchIndex(core.cli_state(ctx).context.paths.index_database).verify(),
        title="Index verification",
        source="search_index",
    )


@index_app.command("optimize")
def index_optimize(ctx: typer.Context) -> None:
    """Compact local FTS segments and refresh SQLite planner statistics."""

    core.run(
        ctx,
        "index optimize",
        lambda: SearchIndex(core.cli_state(ctx).context.paths.index_database).optimize(),
        title="Index optimized",
        source="search_index",
    )


@index_app.command("search")
def index_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    folder: str | None = typer.Option(None),
    fuzzy: bool = typer.Option(False),
    threshold: int = typer.Option(75, min=0, max=100),
    limit: int = typer.Option(50, min=1, max=500),
    offset: int = typer.Option(0, min=0),
) -> None:
    """Search locally with FTS5 or deterministic bounded fuzzy ranking."""

    core.run(
        ctx,
        "index search",
        lambda: SearchIndex(core.cli_state(ctx).context.paths.index_database).search(
            query,
            folder=folder,
            fuzzy=fuzzy,
            fuzzy_threshold=threshold,
            limit=limit,
            offset=offset,
        ),
        title="Local search results",
        source="search_index",
    )


@index_app.command("clear")
def index_clear(ctx: typer.Context, dry_run: bool = typer.Option(False)) -> None:
    """Clear only the disposable local search index."""

    def action() -> Any:
        index = SearchIndex(core.cli_state(ctx).context.paths.index_database)
        if dry_run:
            return {"dry_run": True, "documents": index.stats().documents}
        core.confirm(ctx, "Clear the disposable local full-text index?", "index-clear:v1")
        return {"cleared": index.clear()}

    core.run(ctx, "index clear", action, title="Index clear", source="search_index")


def _semantic_service(ctx: typer.Context) -> SemanticSearchService:
    context = core.cli_state(ctx).context
    enabled = bool(core.settings_service(ctx).value("search.semantic_enabled").effective_value)
    return SemanticSearchService(
        SearchIndex(context.paths.index_database),
        SemanticStore(
            context.paths.data_dir / "semantic.sqlite3",
            identity=account_identity(context.config.email or "", context.config.endpoint),
            enabled=enabled,
        ),
    )


@ocr_app.command("status")
def ocr_status(ctx: typer.Context) -> None:
    """Inspect installed local recognizer languages; never install or recognize an image."""
    core.run(ctx, "ocr status", lambda: OcrService().status(), title="Local OCR", source="local")


@ocr_app.command("extract")
def ocr_extract(
    ctx: typer.Context,
    path: Path = typer.Argument(..., help="One already saved local PNG or JPEG."),
    language: str = typer.Option(..., help="Exact installed tag from ocr status, such as en-US."),
    apply: bool = typer.Option(False, help="Opt in to recognition after reviewing the preview."),
    timeout: float = typer.Option(8, min=1, max=15, help="Owned recognition worker time limit."),
    max_chars: int = typer.Option(50000, min=1, max=100000, help="Maximum recognized output."),
) -> None:
    """Preview a bounded local image; --apply and confirmation recognize it locally."""

    def action() -> Any:
        service = OcrService()
        plan = service.preview(
            path, language=language, timeout_seconds=timeout, max_output_chars=max_chars
        )
        if not apply:
            return plan.as_dict()
        core.confirm(
            ctx,
            str(plan.as_dict()["privacy"]) + " " + str(plan.as_dict()["accuracy"]),
            "ocr:" + plan.sha256 + ":" + plan.language,
        )
        result = service.extract(plan, enabled=True)
        if result.status != "success":
            raise ConfigurationError(result.error or "Local OCR is unavailable. Run ocr status.")
        return result.as_dict()

    core.run(ctx, "ocr extract", action, title="Local OCR", source="local")


@semantic_app.command("status")
def semantic_status(ctx: typer.Context) -> None:
    """Show semantic-search consent and implementation status without network access."""

    core.run(
        ctx,
        "semantic status",
        lambda: _semantic_service(ctx).status(),
        title="Semantic search",
        source="local",
    )


@semantic_app.command("enable")
def semantic_enable(ctx: typer.Context) -> None:
    """Consent to a private corpus-trained model; does not download or build it."""

    def action() -> Any:
        core.confirm(
            ctx,
            "Enable optional local topic search. Building saves vocabulary, title metadata and "
            "embeddings locally; bodies require a separate build opt-in. No remote model is used.",
            "local-semantic-consent",
        )
        result = core.settings_service(ctx).set("search.semantic_enabled", True)
        core.cli_state(ctx).context.reload_config()
        return {
            "enabled": _semantic_service(ctx).store.enabled,
            "revision": result.revision_id if result is not None else None,
        }

    core.run(ctx, "semantic enable", action, title="Semantic consent", source="local")


@semantic_app.command("disable")
def semantic_disable(ctx: typer.Context) -> None:
    """Disable semantic processing while retaining the model until explicitly cleared."""

    def action() -> Any:
        result = core.settings_service(ctx).set("search.semantic_enabled", False)
        core.cli_state(ctx).context.reload_config()
        return {
            "enabled": _semantic_service(ctx).store.enabled,
            "revision": result.revision_id if result is not None else None,
        }

    core.run(ctx, "semantic disable", action, title="Semantic consent", source="local")


@semantic_app.command("build")
@semantic_app.command("update")
def semantic_build(
    ctx: typer.Context,
    apply: bool = typer.Option(False, "--apply", help="Apply the reviewed local build."),
    rebuild: bool = typer.Option(False, "--rebuild", help="Retrain the local corpus-topic model."),
    limit: int | None = typer.Option(
        None, min=2, max=2000, help="Latest local records; initially 500."
    ),
    dimensions: int | None = typer.Option(None, min=1, max=96, help="Initial maximum 32 topics."),
    include_body: bool | None = typer.Option(
        None, "--include-body/--subjects-only", help="Explicitly select indexed body chunks."
    ),
) -> None:
    """Preview/build/update from existing local text. No mailbox fetch or model download."""

    def action() -> Any:
        service = _semantic_service(ctx)
        options: dict[str, Any] = dict(
            limit=limit, dimensions=dimensions, include_body=include_body, rebuild=rebuild
        )
        preview = service.preview(**options)
        if not apply:
            return preview
        core.confirm(
            ctx,
            f"Build local-lsa-v1 from {preview['selected_documents']} local records; "
            f"body chunks: {preview['include_body']}; retrain: {rebuild}. "
            "Save vocabulary, title/folder metadata and embeddings in local semantic storage.",
            "local-semantic-build",
        )
        return service.build(
            confirmed=True, expected_source_digest=preview["source_digest"], **options
        )

    core.run(ctx, "semantic build", action, title="Local topic model", source="local")


@semantic_app.command("search")
def semantic_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    limit: int = typer.Option(50, min=1, max=500),
    semantic_weight: float = typer.Option(0.8, min=0.0, max=1.0),
) -> None:
    """Rank local topic projections and lexical overlap; reject stale indexed text."""

    if not query.strip():
        raise typer.BadParameter("Semantic search needs non-empty text.")
    core.run(
        ctx,
        "semantic search",
        lambda: _semantic_service(ctx).search(query, limit=limit, semantic_weight=semantic_weight),
        title="Local topic matches",
        source="local",
    )


@semantic_app.command("clear")
def semantic_clear(ctx: typer.Context) -> None:
    """Confirm removal of the local topic model and embeddings; retain the lexical index."""

    def action() -> Any:
        core.confirm(
            ctx, "Remove the local semantic model, vocabulary and embeddings.", "semantic-clear"
        )
        return _semantic_service(ctx).clear(confirmed=True)

    core.run(
        ctx,
        "semantic clear",
        action,
        title="Semantic index clear",
        source="local",
    )


def _clusters(
    ctx: typer.Context,
    *,
    folder: str,
    by: str,
    limit: int,
) -> Any:
    try:
        return cluster_messages(
            scan_message_summaries(core.gateway(ctx), folder=folder, limit=limit),
            by=by,
            max_items=limit,
        )
    except (TypeError, ValueError) as exc:
        # An unsupported --by value surfaced as a generic internal error (exit 12).
        raise ConfigurationError(str(exc)) from exc


@clusters_app.command("build")
@clusters_app.command("rebuild")
@clusters_app.command("list")
def clusters_list(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    by: str = typer.Option(
        "topic", help="topic, sender, domain, thread, month, or attachment_type"
    ),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Build deterministic explainable clusters from a bounded mailbox view."""

    core.run(
        ctx,
        "clusters list",
        lambda: _clusters(ctx, folder=folder, by=by, limit=limit),
        title="Mailbox clusters",
    )


@clusters_app.command("show")
def clusters_show(
    ctx: typer.Context,
    cluster_id: str = typer.Argument(...),
    folder: str = typer.Option("inbox"),
    by: str = typer.Option("topic"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Rebuild a bounded view and show one deterministic cluster ID."""

    def action() -> Any:
        for cluster in _clusters(ctx, folder=folder, by=by, limit=limit):
            if cluster.cluster_id == cluster_id:
                return cluster
        raise typer.BadParameter("Cluster ID was not present in the selected bounded view.")

    core.run(ctx, "clusters show", action, title="Mailbox cluster")


@clusters_app.command("search")
def clusters_search(
    ctx: typer.Context,
    query: str = typer.Argument(...),
    folder: str = typer.Option("inbox"),
    by: str = typer.Option("topic"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Filter deterministic cluster labels, explanations, and common terms locally."""

    needle = query.casefold().strip()
    if not needle:
        raise typer.BadParameter("Cluster search needs non-empty text.")

    def action() -> tuple[Any, ...]:
        return tuple(
            cluster
            for cluster in _clusters(ctx, folder=folder, by=by, limit=limit)
            if needle
            in " ".join(
                (
                    cluster.label,
                    cluster.explanation,
                    *(term for term, _count in cluster.common_terms),
                )
            ).casefold()
        )

    core.run(ctx, "clusters search", action, title="Cluster results")


@clusters_app.command("export")
def clusters_export(
    ctx: typer.Context,
    output: Path = typer.Option(...),
    folder: str = typer.Option("inbox"),
    by: str = typer.Option("topic"),
    limit: int = typer.Option(500, min=1, max=500),
    overwrite: bool = typer.Option(False),
) -> None:
    """Export deterministic cluster summaries as canonical JSON."""

    def action() -> dict[str, Any]:
        if output.exists() and not overwrite:
            raise typer.BadParameter("Output exists; use --overwrite after reviewing the path.")
        if output.exists():
            core.confirm(ctx, f"Overwrite cluster export {output}?", f"clusters-export:{output}")
        clusters = _clusters(ctx, folder=folder, by=by, limit=limit)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(
            json.dumps([cluster.as_dict() for cluster in clusters], indent=2, sort_keys=True),
            encoding="utf-8",
            newline="\n",
        )
        return {"path": str(output), "clusters": len(clusters), "by": by}

    core.run(ctx, "clusters export", action, title="Cluster export")


def _insight_source(ctx: typer.Context, *, folder: str, limit: int) -> Any:
    return scan_message_summaries(core.gateway(ctx), folder=folder, limit=limit)


@insights_app.command("overview")
@insights_app.command("analyze")
def insights_analyze(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Analyze storage, senders, and duplicates without changing messages."""

    core.run(
        ctx,
        "insights analyze",
        lambda: analyze_mailbox(
            scan_message_summaries(core.gateway(ctx), folder=folder, limit=limit), max_items=limit
        ),
        title="Mailbox insights",
    )


@insights_app.command("storage")
def insights_storage(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Analyze bounded storage metadata by folder, sender, time, and attachment type."""

    core.run(
        ctx,
        "insights storage",
        lambda: storage_insights(_insight_source(ctx, folder=folder, limit=limit), max_items=limit),
        title="Storage insights",
    )


@insights_app.command("senders")
def insights_senders(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Analyze bounded sender frequency and estimated storage."""

    core.run(
        ctx,
        "insights senders",
        lambda: sender_insights(
            _insight_source(ctx, folder=folder, limit=limit),
            limit=limit,
            max_items=limit,
        ),
        title="Sender insights",
    )


@insights_app.command("duplicates")
def insights_duplicates(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    near: bool = typer.Option(False, help="Also perform bounded near-duplicate comparison."),
    threshold: int = typer.Option(92, min=0, max=100),
    limit: int = typer.Option(500, min=2, max=500),
) -> None:
    """Find exact and optionally bounded near-duplicate messages locally."""

    core.run(
        ctx,
        "insights duplicates",
        lambda: duplicate_insights(
            _insight_source(ctx, folder=folder, limit=limit),
            include_near=near,
            similarity_threshold=threshold,
            max_items=limit,
        ),
        title="Duplicate insights",
    )


@insights_app.command("unanswered")
def insights_unanswered(
    ctx: typer.Context,
    self_address: list[str] = typer.Option(
        [], "--self-address", help="Your sending addresses/aliases."
    ),
    folder: list[str] = typer.Option(["inbox"], "--folder", help="Incoming candidate folders."),
    sent_folder: list[str] = typer.Option(
        ["sent"], "--sent-folder", help="Folders containing sent mail."
    ),
    limit: int = typer.Option(100, min=1, max=1000, help="Metadata records per folder."),
) -> None:
    """Review conversations with no later self message in the bounded folder scope."""

    def action() -> Any:
        from gucmail.services.unanswered import UnansweredService, normalize_self_addresses

        primary = core.cli_state(ctx).context.config.email
        addresses = normalize_self_addresses(([primary] if primary else []) + self_address)
        return UnansweredService(core.gateway(ctx)).analyze(
            self_addresses=addresses,
            folders=folder,
            sent_folders=sent_folder,
            limit=limit,
        )

    core.run(ctx, "insights unanswered", action, title="Unanswered messages")


@insights_app.command("quota")
def insights_quota(ctx: typer.Context) -> None:
    """Show mailbox quota evidence and local disk capacity as separate measurements."""

    core.run(ctx, "insights quota", lambda: core.quota_action(ctx), title="Storage capacity")


def _cleanup_plan(
    ctx: typer.Context,
    *,
    folder: str,
    limit: int,
    include_old_unread: bool,
    old_unread_days: int,
    exclusions: list[str],
) -> Any:
    messages = scan_message_summaries(core.gateway(ctx), folder=folder, limit=limit)
    return CleanupPlanner().analyze(
        messages,
        include_duplicates=True,
        include_old_unread=include_old_unread,
        old_unread_days=old_unread_days,
        exclusions=exclusions,
        max_items=limit,
    )


@cleanup_app.command("analyze")
@cleanup_app.command("plan")
def cleanup_analyze(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    old_unread: bool = typer.Option(False, help="Also include old unread messages."),
    old_unread_days: int = typer.Option(365, min=1),
    exclude: list[str] = typer.Option([], "--exclude"),
    limit: int = typer.Option(500, min=1, max=500),
) -> None:
    """Create an immutable, exact-candidate cleanup preview without mutations."""

    core.run(
        ctx,
        "cleanup analyze",
        lambda: _cleanup_plan(
            ctx,
            folder=folder,
            limit=limit,
            include_old_unread=old_unread,
            old_unread_days=old_unread_days,
            exclusions=exclude,
        ),
        title="Cleanup preview",
    )


@cleanup_app.command("execute")
def cleanup_execute(
    ctx: typer.Context,
    folder: str = typer.Option("inbox"),
    old_unread: bool = typer.Option(False),
    old_unread_days: int = typer.Option(365, min=1),
    exclude: list[str] = typer.Option([], "--exclude"),
    limit: int = typer.Option(500, min=1, max=500),
    apply: bool = typer.Option(False, help="Move the exact previewed candidates to Deleted Items."),
    continue_on_error: bool = typer.Option(False),
) -> None:
    """Preview by default; --apply uses recoverable deletion after exact confirmation."""

    def action() -> Any:
        plan = _cleanup_plan(
            ctx,
            folder=folder,
            limit=limit,
            include_old_unread=old_unread,
            old_unread_days=old_unread_days,
            exclusions=exclude,
        )
        if not apply:
            return plan
        core.confirm(
            ctx,
            f"Move {plan.item_count} exact cleanup candidate(s) to Deleted Items?",
            plan.confirmation_token,
        )
        gateway = core.gateway(ctx)
        return CleanupPlanner().execute(
            plan,
            lambda candidate: gateway.delete_message(candidate.reference, permanent=False),
            confirmed=True,
            continue_on_error=continue_on_error,
        )

    core.run(ctx, "cleanup execute", action, title="Cleanup execution")
