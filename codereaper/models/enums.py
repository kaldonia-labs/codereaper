"""Enumerations used throughout CodeReaper."""

from enum import StrEnum


class ScanStatus(StrEnum):
    """Lifecycle states of a scan."""

    PENDING = "pending"
    EXPLORING = "exploring"
    COLLECTING_COVERAGE = "collecting_coverage"
    COMPLETED = "completed"
    FAILED = "failed"


class AnalysisStatus(StrEnum):
    """Lifecycle states of the analysis phase."""

    PENDING = "pending"
    PARSING_AST = "parsing_ast"
    MAPPING_COVERAGE = "mapping_coverage"
    CROSS_REFERENCING = "cross_referencing"
    COMPLETED = "completed"
    FAILED = "failed"


class RiskScore(StrEnum):
    """Risk classification for dead-code candidates."""

    LOW = "low"          # Utility never called
    MEDIUM = "medium"    # Handler for rare UI path
    HIGH = "high"        # Referenced by string or dynamic import


class SafetyMode(StrEnum):
    """Controls which risk thresholds are included in patches."""

    CONSERVATIVE = "conservative"  # Low-risk only (default)
    BALANCED = "balanced"          # Low + medium
    AGGRESSIVE = "aggressive"      # All candidates


class PatchStatus(StrEnum):
    """Lifecycle states of a patch."""

    GENERATED = "generated"
    APPLIED = "applied"
    VERIFIED = "verified"
    VERIFICATION_FAILED = "verification_failed"
    ROLLED_BACK = "rolled_back"


class VerificationStatus(StrEnum):
    """Lifecycle states of the verification phase."""

    PENDING = "pending"
    REPLAYING = "replaying"
    COMPARING = "comparing"
    PASSED = "passed"
    FAILED = "failed"
