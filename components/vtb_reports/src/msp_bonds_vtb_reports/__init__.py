"""VTB reports component: archive checks and offline import preparation."""
from .archive import (
    ENV_REPORT_ROOT,
    REPORT_CATEGORIES,
    archive_status,
    check_report_files,
    inventory,
    list_reports,
)
from .archive_reconciliation import match_archive_candidate, reconcile_archive, write_archive_registry
from .classifier import ClassificationResult, classify_document
from .import_journal import ImportJournal, JournalResult, default_journal_path
from .mail_routing import MessageRoute, route_message_labels
from .text_extraction import ExtractionResult, extract_document_text

__version__ = "0.1.0"
