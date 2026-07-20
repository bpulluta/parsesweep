"""Tests for CLI features including cost tracking, dashboard, and UI."""

import pytest
from pathlib import Path
from streamline_extract.cli.cost_tracker import CostTracker
from streamline_extract.cli.dashboard import ExtractionDashboard, create_live_dashboard
from streamline_extract.cli.ui import (
    console,
    print_error,
    print_success,
    print_warning,
    print_info,
    create_config_table,
    create_summary_table,
    ask_confirm,
)


class TestCostTracker:
    """Test the CostTracker class."""
    
    def test_initialization(self):
        """Test cost tracker initialization."""
        tracker = CostTracker(model="gpt-4o-mini")
        assert tracker.model == "gpt-4o-mini"
        assert tracker.total_input_tokens == 0
        assert tracker.total_output_tokens == 0
        assert tracker.total_requests == 0
        assert tracker.failed_requests == 0
    
    def test_add_request(self):
        """Test adding requests to tracker."""
        tracker = CostTracker()
        tracker.add_request(1000, 500)
        
        assert tracker.total_input_tokens == 1000
        assert tracker.total_output_tokens == 500
        assert tracker.total_requests == 1
    
    def test_add_multiple_requests(self):
        """Test adding multiple requests."""
        tracker = CostTracker()
        tracker.add_request(1000, 500)
        tracker.add_request(2000, 1000)
        tracker.add_request(1500, 750)
        
        assert tracker.total_input_tokens == 4500
        assert tracker.total_output_tokens == 2250
        assert tracker.total_requests == 3
    
    def test_add_failure(self):
        """Test tracking failed requests."""
        tracker = CostTracker()
        tracker.add_request(1000, 500)
        tracker.add_failure()
        tracker.add_failure()
        
        assert tracker.total_requests == 1
        assert tracker.failed_requests == 2
    
    def test_cost_calculation_gpt4o_mini(self):
        """Test cost calculation for gpt-4o-mini."""
        tracker = CostTracker(model="gpt-4o-mini")
        # Add 1M input tokens and 1M output tokens
        tracker.add_request(1_000_000, 1_000_000)
        
        input_cost = tracker.get_input_cost()
        output_cost = tracker.get_output_cost()
        total_cost = tracker.get_total_cost()
        
        # gpt-4o-mini: $0.15/1M input, $0.60/1M output
        assert input_cost == 0.15
        assert output_cost == 0.60
        assert total_cost == 0.75
    
    def test_cost_calculation_uses_shared_pricing_db(self):
        """CostTracker rates come from the single shared pricing DB.

        Regression guard for the pricing consolidation: CostTracker no longer
        carries its own private price table, so rates must match
        ``utils.model_pricing`` for any model (here gpt-4o = $5/$15 per 1M).
        """
        tracker = CostTracker(model="gpt-4o")
        tracker.add_request(1_000_000, 1_000_000)

        # gpt-4o: $5.00/1M input, $15.00/1M output (from model_pricing DB)
        assert tracker.get_input_cost() == 5.00
        assert tracker.get_output_cost() == 15.00
        assert tracker.get_total_cost() == 20.00
    
    def test_average_cost_per_document(self):
        """Test average cost calculation."""
        tracker = CostTracker(model="gpt-4o-mini")
        
        # Add 3 successful requests
        tracker.add_request(100_000, 50_000)  # $0.045
        tracker.add_request(200_000, 100_000)  # $0.090
        tracker.add_request(150_000, 75_000)  # $0.0675
        
        # Total: $0.2025, Average: $0.0675
        avg = tracker.get_average_cost_per_document()
        assert abs(avg - 0.0675) < 0.0001
    
    def test_average_cost_with_failures(self):
        """Test average cost with failed requests."""
        tracker = CostTracker(model="gpt-4o-mini")
        
        tracker.add_request(100_000, 50_000)
        tracker.add_failure()
        tracker.add_request(100_000, 50_000)
        
        # 2 requests total, 1 failed, so 1 successful
        # successful_requests = total_requests - failed_requests = 2 - 1 = 1
        avg = tracker.get_average_cost_per_document()
        total = tracker.get_total_cost()
        
        # Average should be total / successful (not total requests)
        assert avg == total / 1
        assert avg == total  # Since only 1 successful
    
    def test_get_summary(self):
        """Test summary statistics."""
        tracker = CostTracker(model="gpt-4o-mini")
        tracker.add_request(1000, 500)
        tracker.add_request(2000, 1000)
        tracker.add_failure()
        
        summary = tracker.get_summary()
        
        # 2 requests via add_request(), 1 failure via add_failure()
        # successful = total_requests - failed_requests = 2 - 1 = 1
        assert summary['total_requests'] == 2
        assert summary['successful_requests'] == 1
        assert summary['failed_requests'] == 1
        assert summary['total_input_tokens'] == 3000
        assert summary['total_output_tokens'] == 1500
        assert summary['total_tokens'] == 4500
        assert 'total_cost' in summary
        assert 'avg_cost_per_doc' in summary
        assert summary['model'] == "gpt-4o-mini"
    
    def test_document_costs(self):
        """Test per-document cost tracking."""
        tracker = CostTracker()
        tracker.add_request(1000, 500, document_name="doc1.pdf", cost=0.05)
        tracker.add_request(2000, 1000, document_name="doc2.pdf", cost=0.10)
        
        assert tracker.document_costs["doc1.pdf"] == 0.05
        assert tracker.document_costs["doc2.pdf"] == 0.10


class TestExtractionDashboard:
    """Test the ExtractionDashboard class."""
    
    def test_initialization(self):
        """Test dashboard initialization."""
        dashboard = ExtractionDashboard(total_documents=10)
        
        assert dashboard.total_documents == 10
        assert dashboard.processed == 0
        assert dashboard.successful == 0
        assert dashboard.failed == 0
        assert dashboard.current_document is None
    
    def test_start_document(self):
        """Test starting document processing."""
        dashboard = ExtractionDashboard(total_documents=5)
        dashboard.start_document("test.pdf")
        
        assert dashboard.current_document == "test.pdf"
    
    def test_complete_document_success(self):
        """Test completing a document successfully."""
        dashboard = ExtractionDashboard(total_documents=5)
        dashboard.start_document("test.pdf")
        dashboard.complete_document(
            "test.pdf",
            success=True,
            cost=0.05,
            input_tokens=1000,
            output_tokens=500
        )
        
        assert dashboard.processed == 1
        assert dashboard.successful == 1
        assert dashboard.failed == 0
        assert dashboard.current_document is None
    
    def test_complete_document_failure(self):
        """Test completing a document with failure."""
        dashboard = ExtractionDashboard(total_documents=5)
        dashboard.start_document("test.pdf")
        dashboard.complete_document("test.pdf", success=False)
        
        assert dashboard.processed == 1
        assert dashboard.successful == 0
        assert dashboard.failed == 1
    
    def test_multiple_documents(self):
        """Test processing multiple documents."""
        dashboard = ExtractionDashboard(total_documents=3)
        
        dashboard.start_document("doc1.pdf")
        dashboard.complete_document("doc1.pdf", success=True, cost=0.05, input_tokens=1000, output_tokens=500)
        
        dashboard.start_document("doc2.pdf")
        dashboard.complete_document("doc2.pdf", success=False)
        
        dashboard.start_document("doc3.pdf")
        dashboard.complete_document("doc3.pdf", success=True, cost=0.03, input_tokens=800, output_tokens=400)
        
        assert dashboard.processed == 3
        assert dashboard.successful == 2
        assert dashboard.failed == 1
    
    def test_get_final_summary(self):
        """Test getting final summary."""
        dashboard = ExtractionDashboard(total_documents=2, model="gpt-4o-mini")
        dashboard.start_document("doc1.pdf")
        dashboard.complete_document("doc1.pdf", success=True, cost=0.05, input_tokens=1000, output_tokens=500)
        
        summary = dashboard.get_final_summary()
        
        assert summary['processed'] == 1
        assert summary['successful'] == 1
        assert summary['failed'] == 0
        assert 'total_cost' in summary
        assert 'model' in summary


class TestCreateLiveDashboard:
    """Test the create_live_dashboard helper function."""
    
    def test_create_dashboard(self):
        """Test creating a live dashboard."""
        live, dashboard = create_live_dashboard(total_documents=10, model="gpt-4o-mini")
        
        assert dashboard.total_documents == 10
        assert dashboard.cost_tracker.model == "gpt-4o-mini"
        assert live is not None


class TestUIFunctions:
    """Test UI utility functions."""
    
    def test_create_config_table(self):
        """Test creating a config table."""
        config = {
            "Documents": "10 files",
            "Schema": "test_schema.json",
            "Output": "extracted/test/",
        }
        
        table = create_config_table("Test Config", config)
        assert table is not None
        assert table.title == "📄 Test Config"
    
    def test_create_summary_table(self):
        """Test creating a summary table."""
        stats = {
            "Processed": 10,
            "Successful": 9,
            "Failed": 1,
            "Total Cost": "$0.50",
        }
        
        table = create_summary_table("Test Summary", stats)
        assert table is not None
        assert table.title == "📊 Test Summary"


class TestCostCalculationAccuracy:
    """Test accuracy of cost calculations."""
    
    def test_small_request_cost(self):
        """Test cost calculation for small requests."""
        tracker = CostTracker(model="gpt-4o-mini")
        # 10,000 input tokens, 5,000 output tokens
        tracker.add_request(10_000, 5_000)
        
        # Expected: (10000/1000000)*0.15 + (5000/1000000)*0.60
        # = 0.0015 + 0.003 = 0.0045
        assert abs(tracker.get_total_cost() - 0.0045) < 0.00001
    
    def test_large_request_cost(self):
        """Test cost calculation for large requests."""
        tracker = CostTracker(model="gpt-4o-mini")
        # 10M input tokens, 1M output tokens
        tracker.add_request(10_000_000, 1_000_000)
        
        # Expected: (10M/1M)*0.15 + (1M/1M)*0.60
        # = 10*0.15 + 1*0.60 = 1.5 + 0.6 = 2.1
        assert abs(tracker.get_total_cost() - 2.1) < 0.00001
    
    def test_accumulated_cost_accuracy(self):
        """Test accumulated cost accuracy over many requests."""
        tracker = CostTracker(model="gpt-4o-mini")
        
        # Add 100 small requests
        for _ in range(100):
            tracker.add_request(1_000, 500)
        
        # Expected: 100 * ((1000/1M)*0.15 + (500/1M)*0.60)
        # = 100 * (0.00015 + 0.0003) = 100 * 0.00045 = 0.045
        assert abs(tracker.get_total_cost() - 0.045) < 0.00001


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
