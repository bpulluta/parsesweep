"""Production logging system with progress tracking and cost reporting."""

import logging
import sys
import time
from pathlib import Path
from typing import Optional
from dataclasses import dataclass, field


@dataclass
class ExtractionMetrics:
    """Track metrics for extraction operations."""

    total_files: int = 0
    successful: int = 0
    failed: int = 0
    skipped: int = 0
    total_cost: float = 0.0
    total_time: float = 0.0
    start_time: float = field(default_factory=time.time)

    def add_success(self, cost: float, duration: float):
        """Record a successful extraction."""
        self.successful += 1
        self.total_cost += cost
        self.total_time += duration

    def add_failure(self):
        """Record a failed extraction."""
        self.failed += 1

    def add_skip(self):
        """Record a skipped file."""
        self.skipped += 1

    def get_elapsed_time(self) -> float:
        """Get total elapsed time."""
        return time.time() - self.start_time

    def get_avg_time_per_file(self) -> float:
        """Get average processing time per successful file."""
        if self.successful == 0:
            return 0.0
        return self.total_time / self.successful

    def get_summary(self) -> dict:
        """Get summary statistics."""
        elapsed = self.get_elapsed_time()
        return {
            "total_files": self.total_files,
            "successful": self.successful,
            "failed": self.failed,
            "skipped": self.skipped,
            "total_cost_usd": round(self.total_cost, 4),
            "avg_cost_per_file": round(
                self.total_cost / max(self.successful, 1), 4
            ),
            "total_time_sec": round(elapsed, 2),
            "avg_time_per_file_sec": round(self.get_avg_time_per_file(), 2),
        }


class ProductionLogger:
    """
    Production-ready logger with structured output and progress tracking.

    Features:

    - Colored console output
    - Progress indicators
    - Cost tracking
    - Time reporting
    - File logging
    """

    # Color codes
    COLORS = {
        "RESET": "\033[0m",
        "BOLD": "\033[1m",
        "RED": "\033[91m",
        "GREEN": "\033[92m",
        "YELLOW": "\033[93m",
        "BLUE": "\033[94m",
        "MAGENTA": "\033[95m",
        "CYAN": "\033[96m",
    }

    def __init__(
        self, name: str, log_file: Optional[Path] = None, verbose: bool = False
    ):
        """
        Initialize production logger.

        Parameters
        ----------
        name : str
            Logger name
        log_file : Optional[Path]
            Optional file path for log output
        verbose : bool
            Enable verbose (DEBUG) logging
        """
        self.logger = logging.getLogger(name)
        self.logger.setLevel(logging.DEBUG if verbose else logging.INFO)
        self.logger.handlers.clear()

        # Console handler with formatting
        console_handler = logging.StreamHandler(sys.stdout)
        console_handler.setLevel(logging.DEBUG if verbose else logging.INFO)
        console_formatter = logging.Formatter(
            "%(message)s"  # Simple format for console
        )
        console_handler.setFormatter(console_formatter)
        self.logger.addHandler(console_handler)

        # File handler if specified
        if log_file:
            log_file.parent.mkdir(parents=True, exist_ok=True)
            file_handler = logging.FileHandler(log_file)
            file_handler.setLevel(logging.DEBUG)
            file_formatter = logging.Formatter(
                "%(asctime)s - %(name)s - %(levelname)s - %(message)s"
            )
            file_handler.setFormatter(file_formatter)
            self.logger.addHandler(file_handler)

        self.verbose = verbose

    def _colorize(self, text: str, color: str) -> str:
        """Add color to text."""
        return f"{self.COLORS.get(color, '')}{text}{self.COLORS['RESET']}"

    def header(self, text: str):
        """Print a bold header."""
        self.logger.info(f"\n{self._colorize('=' * 80, 'BOLD')}")
        self.logger.info(self._colorize(text.upper(), "BOLD"))
        self.logger.info(self._colorize("=" * 80, "BOLD"))

    def section(self, text: str):
        """Print a section header."""
        self.logger.info(f"\n{self._colorize(text, 'CYAN')}")
        self.logger.info(self._colorize("-" * len(text), "CYAN"))

    def success(self, text: str):
        """Print success message."""
        self.logger.info(self._colorize(f"✓ {text}", "GREEN"))

    def error(self, text: str):
        """Print error message."""
        self.logger.error(self._colorize(f"✗ {text}", "RED"))

    def warning(self, text: str):
        """Print warning message."""
        self.logger.warning(self._colorize(f"⚠ {text}", "YELLOW"))

    def info(self, text: str):
        """Print info message."""
        self.logger.info(text)

    def debug(self, text: str):
        """Print debug message."""
        self.logger.debug(self._colorize(f"🔍 {text}", "MAGENTA"))

    def progress(self, current: int, total: int, text: str = ""):
        """Print progress indicator."""
        percentage = (current / total * 100) if total > 0 else 0
        bar_length = 40
        filled = int(bar_length * current / total) if total > 0 else 0
        bar = "█" * filled + "░" * (bar_length - filled)

        progress_text = f"[{current}/{total}] {bar} {percentage:.1f}%"
        if text:
            progress_text += f" - {text}"

        self.logger.info(self._colorize(progress_text, "BLUE"))

    def metrics_summary(self, metrics: ExtractionMetrics):
        """Print extraction metrics summary."""
        self.header("EXTRACTION COMPLETE")

        summary = metrics.get_summary()

        self.info(f"Total Files:     {summary['total_files']}")
        self.info(
            f"Successful:      {self._colorize(str(summary['successful']), 'GREEN')}"
        )
        if summary["failed"] > 0:
            self.info(
                f"Failed:          {self._colorize(str(summary['failed']), 'RED')}"
            )
        if summary["skipped"] > 0:
            self.info(
                f"Skipped:         {self._colorize(str(summary['skipped']), 'YELLOW')}"
            )

        self.info("")
        self.info(f"Total Cost:      ${summary['total_cost_usd']:.4f}")
        self.info(f"Avg Cost/File:   ${summary['avg_cost_per_file']:.4f}")

        self.info("")
        self.info(f"Total Time:      {summary['total_time_sec']:.2f}s")
        self.info(f"Avg Time/File:   {summary['avg_time_per_file_sec']:.2f}s")

        self.logger.info(self._colorize("=" * 80, "BOLD"))

    def file_result(
        self,
        filename: str,
        status: str,
        cost: float = 0.0,
        duration: float = 0.0,
        details: str = "",
    ):
        """Log result for a single file."""
        if status == "success":
            msg = f"✓ {filename}"
            if cost > 0:
                msg += f" (${cost:.4f}, {duration:.2f}s)"
            if details:
                msg += f" - {details}"
            self.success(msg)
        elif status == "failed":
            msg = f"✗ {filename}"
            if details:
                msg += f" - {details}"
            self.error(msg)
        elif status == "skipped":
            msg = f"⊘ {filename}"
            if details:
                msg += f" - {details}"
            self.warning(msg)


def get_logger(
    name: str = "psweep",
    log_file: Optional[Path] = None,
    verbose: bool = False,
) -> ProductionLogger:
    """
    Get a production logger instance.

    Parameters
    ----------
    name : str
        Logger name
    log_file : Optional[Path]
        Optional file path for log output
    verbose : bool
        Enable verbose (DEBUG) logging

    Returns
    -------
    ProductionLogger
        ProductionLogger instance
    """
    return ProductionLogger(name, log_file, verbose)
