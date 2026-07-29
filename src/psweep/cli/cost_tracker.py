"""Cost tracking utilities for ParseSweep CLI."""

from typing import Dict, Optional
from dataclasses import dataclass, field
from datetime import datetime

from psweep.extraction.llm_factory import DEFAULT_MODEL
from psweep.utils.model_pricing import get_model_pricing


@dataclass
class CostTracker:
    """Track API costs in real-time during extraction operations."""

    model: str = DEFAULT_MODEL
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    total_requests: int = 0
    failed_requests: int = 0
    start_time: Optional[datetime] = None
    document_costs: Dict[str, float] = field(default_factory=dict)

    def __post_init__(self):
        """Initialize start time."""
        if self.start_time is None:
            self.start_time = datetime.now()

    def add_request(
        self,
        input_tokens: int,
        output_tokens: int,
        document_name: str = None,
        cost: float = None,
    ):
        """
        Add a request to the tracker.

        Args:
            input_tokens: Number of input tokens used
            output_tokens: Number of output tokens generated
            document_name: Optional document identifier
            cost: Optional explicit cost (if provided, used instead of calculation)
        """
        self.total_input_tokens += input_tokens
        self.total_output_tokens += output_tokens
        self.total_requests += 1

        if document_name and cost is not None:
            self.document_costs[document_name] = cost

    def add_failure(self):
        """Record a failed request."""
        self.failed_requests += 1

    def get_input_cost(self) -> float:
        """Calculate total input cost (rates from the shared pricing DB)."""
        input_rate, _ = get_model_pricing(self.model)
        return (self.total_input_tokens / 1_000_000) * input_rate

    def get_output_cost(self) -> float:
        """Calculate total output cost (rates from the shared pricing DB)."""
        _, output_rate = get_model_pricing(self.model)
        return (self.total_output_tokens / 1_000_000) * output_rate

    def get_total_cost(self) -> float:
        """Calculate total cost."""
        return self.get_input_cost() + self.get_output_cost()

    def get_average_cost_per_document(self) -> float:
        """Get average cost per successfully processed document."""
        successful = self.total_requests - self.failed_requests
        if successful == 0:
            return 0.0
        return self.get_total_cost() / successful

    def get_elapsed_time(self) -> float:
        """Get elapsed time in seconds."""
        if self.start_time is None:
            return 0.0
        return (datetime.now() - self.start_time).total_seconds()

    def get_summary(self) -> Dict[str, any]:
        """Get a summary of tracked costs and statistics."""
        elapsed = self.get_elapsed_time()
        successful = self.total_requests - self.failed_requests

        return {
            "total_requests": self.total_requests,
            "successful_requests": successful,
            "failed_requests": self.failed_requests,
            "total_input_tokens": self.total_input_tokens,
            "total_output_tokens": self.total_output_tokens,
            "total_tokens": self.total_input_tokens + self.total_output_tokens,
            "input_cost": self.get_input_cost(),
            "output_cost": self.get_output_cost(),
            "total_cost": self.get_total_cost(),
            "avg_cost_per_doc": self.get_average_cost_per_document(),
            "elapsed_seconds": elapsed,
            "model": self.model,
        }

    def format_summary(self) -> str:
        """Format summary as a readable string."""
        summary = self.get_summary()

        lines = [
            f"Model: {summary['model']}",
            f"Requests: {summary['successful_requests']}/{summary['total_requests']} successful",
            f"Tokens: {summary['total_tokens']:,} ({summary['total_input_tokens']:,} in, {summary['total_output_tokens']:,} out)",
            f"Cost: ${summary['total_cost']:.4f} (${summary['input_cost']:.4f} in, ${summary['output_cost']:.4f} out)",
            f"Avg per doc: ${summary['avg_cost_per_doc']:.4f}",
            f"Time: {summary['elapsed_seconds']:.1f}s",
        ]

        if summary["failed_requests"] > 0:
            lines.append(f"⚠ Failed: {summary['failed_requests']}")

        return "\n".join(lines)

    def reset(self):
        """Reset the tracker."""
        self.total_input_tokens = 0
        self.total_output_tokens = 0
        self.total_requests = 0
        self.failed_requests = 0
        self.start_time = datetime.now()
        self.document_costs.clear()
