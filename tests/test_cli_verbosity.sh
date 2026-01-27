#!/bin/bash
# Test script to verify CLI verbosity options work correctly
# Tests both process and consolidate commands with all verbosity levels

# Don't exit on error - we expect some commands to fail due to missing API keys
# set -e

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Test counters
TESTS_PASSED=0
TESTS_FAILED=0

# Helper functions
print_test() {
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}TEST: $1${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
}

pass() {
    echo -e "${GREEN}✓ PASS${NC}: $1"
    ((TESTS_PASSED++))
}

fail() {
    echo -e "${RED}✗ FAIL${NC}: $1"
    ((TESTS_FAILED++))
}

check_contains() {
    local output="$1"
    local expected="$2"
    local test_name="$3"
    
    if [[ "$output" == *"$expected"* ]]; then
        pass "$test_name"
        return 0
    else
        fail "$test_name - Expected to find: '$expected'"
        echo "Output was:"
        echo "$output" | head -20
        return 1
    fi
}

check_not_contains() {
    local output="$1"
    local unexpected="$2"
    local test_name="$3"
    
    if [[ "$output" != *"$unexpected"* ]]; then
        pass "$test_name"
        return 0
    else
        fail "$test_name - Should NOT contain: '$unexpected'"
        echo "Output was:"
        echo "$output" | head -20
        return 1
    fi
}

# Setup test environment
setup_test_env() {
    echo -e "\n${YELLOW}Setting up test environment...${NC}"
    
    # Create test directories
    mkdir -p test_verbosity/documents
    mkdir -p test_verbosity/processed
    mkdir -p test_verbosity/consolidated
    
    # Create sample text files (to avoid needing actual PDFs)
    cat > test_verbosity/documents/sample1.txt << 'EOF'
Sample Rate Schedule

Utility: Test Electric Company
Rate Name: Residential Basic Service
Effective Date: 2024-01-01

Energy Charges:
- Summer (June-Sept): $0.15/kWh
- Winter (Oct-May): $0.12/kWh

Customer Charge: $12.50/month
EOF

    cat > test_verbosity/documents/sample2.txt << 'EOF'
Commercial Rate Schedule

Utility: Test Electric Company  
Rate Name: Commercial General Service
Effective Date: 2024-01-01

Energy Charges:
- Peak: $0.18/kWh
- Off-Peak: $0.10/kWh

Demand Charge: $15.00/kW
Customer Charge: $25.00/month
EOF

    cat > test_verbosity/documents/sample3.txt << 'EOF'
Industrial Rate Schedule

Utility: Test Electric Company
Rate Name: Industrial Large Power
Effective Date: 2024-01-01

Energy Charges:
- On-Peak: $0.20/kWh
- Off-Peak: $0.11/kWh

Demand Charge: $18.00/kW
Customer Charge: $100.00/month
EOF

    echo -e "${GREEN}✓ Test environment created${NC}"
}

# Cleanup test environment
cleanup_test_env() {
    echo -e "\n${YELLOW}Cleaning up test environment...${NC}"
    rm -rf test_verbosity
    echo -e "${GREEN}✓ Cleanup complete${NC}"
}

# Test help output
test_help() {
    print_test "Verify --help shows verbosity options"
    
    local output
    output=$(pixi run streamline-extract process --help 2>&1)
    
    check_contains "$output" "--quiet" "process --help shows --quiet option"
    check_contains "$output" "--verbose" "process --help shows --verbose option"
    check_contains "$output" "--debug" "process --help shows --debug option"
    
    output=$(pixi run streamline-extract consolidate --help 2>&1)
    
    check_contains "$output" "--quiet" "consolidate --help shows --quiet option"
    check_contains "$output" "--verbose" "consolidate --help shows --verbose option"
    check_contains "$output" "--debug" "consolidate --help shows --debug option"
}

# Test process command with different verbosity levels
test_process_quiet() {
    print_test "Process command with --quiet flag"
    
    local output
    output=$(pixi run streamline-extract process test_verbosity/documents/ --quiet --reprocess 2>&1 || true)
    
    # Quiet mode should NOT show headers, tables, or progress
    check_not_contains "$output" "DOCUMENT EXTRACTION" "Quiet mode: no header"
    check_not_contains "$output" "Configuration" "Quiet mode: no configuration table"
    check_not_contains "$output" "Processing" "Quiet mode: no processing messages"
    
    # Should show errors if any (but in our case might just fail due to no API key)
    echo "Output snippet:"
    echo "$output" | head -10
}

test_process_normal() {
    print_test "Process command with default/normal verbosity"
    
    local output
    output=$(pixi run streamline-extract process test_verbosity/documents/ --reprocess 2>&1 || true)
    
    # Normal mode should show headers and configuration
    check_contains "$output" "Input" "Normal mode: shows configuration"
    
    echo "Output snippet:"
    echo "$output" | head -20
}

test_process_verbose() {
    print_test "Process command with --verbose flag"
    
    local output
    output=$(pixi run streamline-extract process test_verbosity/documents/ --verbose --reprocess 2>&1 || true)
    
    # Verbose mode should show configuration
    check_contains "$output" "Input" "Verbose mode: shows configuration"
    
    echo "Output snippet:"
    echo "$output" | head -20
}

test_process_debug() {
    print_test "Process command with --debug flag"
    
    local output
    output=$(pixi run streamline-extract process test_verbosity/documents/ --debug --reprocess 2>&1 || true)
    
    # Debug mode should show configuration
    check_contains "$output" "Input" "Debug mode: shows configuration"
    
    echo "Output snippet:"
    echo "$output" | head -20
}

# Test consolidate command (create mock processed files first)
test_consolidate_setup() {
    echo -e "\n${YELLOW}Creating mock processed files for consolidate tests...${NC}"
    
    # Create mock extraction JSON files
    cat > test_verbosity/processed/sample1.json << 'EOF'
{
  "source_file": "sample1.txt",
  "extraction_date": "2024-01-01 12:00:00",
  "state": "test",
  "model": "gpt-4o-mini",
  "qa_qc_enabled": false,
  "cost_usd": 0.01,
  "processing_time_sec": 2.5,
  "completeness_score": 0.95,
  "item_count": 2,
  "identifier": "test-1",
  "data": {
    "utility_info": {
      "utility_name": "Test Electric Company",
      "state": "CA"
    },
    "rate_schedules": [
      {
        "rate_name": "Residential Basic",
        "customer_charge": 12.50,
        "energy_charge_summer": 0.15,
        "energy_charge_winter": 0.12
      }
    ]
  },
  "validation_notes": []
}
EOF

    cat > test_verbosity/processed/sample2.json << 'EOF'
{
  "source_file": "sample2.txt",
  "extraction_date": "2024-01-01 12:01:00",
  "state": "test",
  "model": "gpt-4o-mini",
  "qa_qc_enabled": false,
  "cost_usd": 0.01,
  "processing_time_sec": 2.3,
  "completeness_score": 0.93,
  "item_count": 2,
  "identifier": "test-2",
  "data": {
    "utility_info": {
      "utility_name": "Test Electric Company",
      "state": "CA"
    },
    "rate_schedules": [
      {
        "rate_name": "Commercial General",
        "customer_charge": 25.00,
        "demand_charge": 15.00,
        "energy_charge_peak": 0.18,
        "energy_charge_offpeak": 0.10
      }
    ]
  },
  "validation_notes": []
}
EOF

    echo -e "${GREEN}✓ Mock processed files created${NC}"
}

test_consolidate_quiet() {
    print_test "Consolidate command with --quiet flag"
    
    local output
    output=$(pixi run streamline-extract consolidate test_verbosity/processed/ --quiet 2>&1 || true)
    
    # Quiet mode should NOT show headers or tables
    check_not_contains "$output" "CONSOLIDATION" "Quiet mode: no header"
    check_not_contains "$output" "Configuration" "Quiet mode: no configuration"
    check_not_contains "$output" "Summary" "Quiet mode: no summary"
    
    echo "Output snippet:"
    echo "$output" | head -10
}

test_consolidate_normal() {
    print_test "Consolidate command with default/normal verbosity"
    
    local output
    output=$(pixi run streamline-extract consolidate test_verbosity/processed/ 2>&1 || true)
    
    # Normal mode should show headers
    check_contains "$output" "Input" "Normal mode: shows input info"
    
    echo "Output snippet:"
    echo "$output" | head -20
}

test_consolidate_verbose() {
    print_test "Consolidate command with --verbose flag"
    
    local output
    output=$(pixi run streamline-extract consolidate test_verbosity/processed/ --verbose 2>&1 || true)
    
    # Verbose mode should show configuration
    check_contains "$output" "Input" "Verbose mode: shows configuration"
    
    echo "Output snippet:"
    echo "$output" | head -20
}

test_consolidate_debug() {
    print_test "Consolidate command with --debug flag"
    
    local output
    output=$(pixi run streamline-extract consolidate test_verbosity/processed/ --debug 2>&1 || true)
    
    # Debug mode should show configuration
    check_contains "$output" "Input" "Debug mode: shows configuration"
    
    echo "Output snippet:"
    echo "$output" | head -20
}

# Main test execution
main() {
    echo -e "${BLUE}"
    echo "╔════════════════════════════════════════════════════════════════════════════════╗"
    echo "║                      CLI VERBOSITY OPTIONS TEST SUITE                         ║"
    echo "╚════════════════════════════════════════════════════════════════════════════════╝"
    echo -e "${NC}"
    
    # Setup
    setup_test_env
    
    # Run tests
    test_help
    test_process_quiet
    test_process_normal
    test_process_verbose
    test_process_debug
    
    test_consolidate_setup
    test_consolidate_quiet
    test_consolidate_normal
    test_consolidate_verbose
    test_consolidate_debug
    
    # Cleanup
    cleanup_test_env
    
    # Summary
    echo -e "\n${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${BLUE}TEST SUMMARY${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    echo -e "${GREEN}Tests Passed: $TESTS_PASSED${NC}"
    echo -e "${RED}Tests Failed: $TESTS_FAILED${NC}"
    echo -e "${BLUE}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
    
    if [ $TESTS_FAILED -eq 0 ]; then
        echo -e "\n${GREEN}✅ ALL TESTS PASSED!${NC}\n"
        return 0
    else
        echo -e "\n${RED}❌ SOME TESTS FAILED${NC}\n"
        return 1
    fi
}

# Run tests
main
