#!/bin/bash
# Setup script for permit-toolkit

set -e

echo "=================================="
echo "Permit Toolkit - Setup"
echo "=================================="
echo ""

# Check Python version
echo "Checking Python version..."
if ! command -v python3 &> /dev/null; then
    echo "❌ Python 3 not found. Please install Python 3.9 or later."
    exit 1
fi

python_version=$(python3 --version 2>&1 | awk '{print $2}')
python_major=$(echo "$python_version" | cut -d. -f1)
python_minor=$(echo "$python_version" | cut -d. -f2)

echo "Found Python $python_version"

# Check if version is 3.9 or later
if [ "$python_major" -lt 3 ] || ([ "$python_major" -eq 3 ] && [ "$python_minor" -lt 9 ]); then
    echo "❌ Python 3.9 or later is required. You have Python $python_version"
    exit 1
fi

echo "✓ Python version is compatible"

# Create virtual environment if it doesn't exist
if [ ! -d ".venv" ]; then
    echo ""
    echo "Creating virtual environment..."
    python3 -m venv .venv
    echo "✓ Virtual environment created"
else
    echo ""
    echo "✓ Virtual environment already exists"
fi

# Activate virtual environment
echo ""
echo "Activating virtual environment..."
source .venv/bin/activate

# Upgrade pip
echo ""
echo "Upgrading pip..."
pip install --upgrade pip > /dev/null

# Install package in editable mode
echo ""
echo "Installing permit-toolkit..."
pip install -e . > /dev/null
echo "✓ Package installed"

# Install development dependencies
read -p "Install development dependencies? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    pip install -e ".[dev]" > /dev/null
    echo "✓ Development dependencies installed"
fi

# Create data directories
echo ""
echo "Creating data directories..."
mkdir -p data/permits
mkdir -p data/extracted
mkdir -p data/outputs
echo "✓ Data directories created"

# Create .env file if it doesn't exist
if [ ! -f ".env" ]; then
    echo ""
    echo "Creating .env file..."
    cat > .env << 'EOF'
# OpenAI API Key (required for extraction)
OPENAI_API_KEY=your_api_key_here

# Optional: Override default paths
# PROJECT_ROOT=/path/to/project
EOF
    echo "✓ .env file created"
    echo ""
    echo "⚠️  IMPORTANT: Edit .env and add your OPENAI_API_KEY"
else
    echo ""
    echo "✓ .env file already exists"
fi

# Archive old structure
echo ""
read -p "Archive old directory structure? (y/N) " -n 1 -r
echo
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo "Archiving old scripts..."
    
    # Move old scripts to archive (if they exist)
    if [ -d "05_scripts" ]; then
        mkdir -p archive/old_scripts
        mv 05_scripts/* archive/old_scripts/ 2>/dev/null || true
        rmdir 05_scripts 2>/dev/null || true
        echo "✓ Archived 05_scripts/"
    fi
    
    # Move old docs
    if [ -d "docs/schemas" ]; then
        mkdir -p archive/old_docs
        mv docs/* archive/old_docs/ 2>/dev/null || true
        echo "✓ Archived old docs/"
    fi
    
    # Archive old data lists
    if [ -d "02_data_lists" ]; then
        mkdir -p archive/old_data
        mv 02_data_lists archive/old_data/ 2>/dev/null || true
        mv 01_data_sources archive/old_data/ 2>/dev/null || true
        echo "✓ Archived old data lists"
    fi
    
    echo ""
    echo "✓ Old structure archived to archive/ directory"
fi

echo ""
echo "=================================="
echo "Setup Complete!"
echo "=================================="
echo ""
echo "Next steps:"
echo "1. Edit .env and add your OPENAI_API_KEY"
echo "2. Try the CLI: permit-toolkit --help"
echo "3. Run an example: python examples/basic_usage.py"
echo ""
echo "To activate the virtual environment in the future:"
echo "  source .venv/bin/activate"
echo ""
echo "For migration help, see MIGRATION.md"
echo ""
