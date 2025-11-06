#!/usr/bin/env python3
"""Sample map visualization using cleaned data."""

from src.permit_toolkit.visualization.mapper import PermitMapper
from pathlib import Path

# Initialize mapper with cleaned data
mapper = PermitMapper(data_dir=Path('data/cleaned'))

# Load data from a few states
df = mapper.load_permits(['Virginia', 'Maryland', 'Ohio'], limit_per_state=20)

print(f'Loaded {len(df)} permits')
print(f'States: {df["state"].value_counts().to_dict()}')

# Geocode facilities
print('\nGeocoding facilities...')
df_geocoded = mapper.geocode_facilities(df, show_progress=True)

# Count successful geocodes
mapped = len(df_geocoded[df_geocoded['latitude'].notna()])
print(f'\nSuccessfully geocoded: {mapped}/{len(df)} ({mapped/len(df)*100:.1f}%')

# Create dashboard
output = Path('data/visualizations/sample_map.html')
output.parent.mkdir(parents=True, exist_ok=True)

mapper.create_dashboard(df_geocoded, output, title='Sample Permit Map - VA, MD, OH')
print(f'\n✓ Dashboard created: {output}')
print(f'\nOpen {output.absolute()} in your browser to view the map.')
