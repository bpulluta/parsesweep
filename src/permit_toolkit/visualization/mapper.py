"""
Interactive map generation for data center facilities with generators.

Creates HTML maps using Folium with:
- Facility locations (geocoded from addresses)
- Generator counts (visualized with color and size)
- Interactive tooltips and popups
- Marker clustering for performance at scale
"""

import json
import re
import ssl
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import certifi
import folium
import pandas as pd
from folium import plugins
from geopy.exc import GeocoderServiceError, GeocoderTimedOut
from geopy.geocoders import Nominatim
from tqdm import tqdm


class FacilityMapper:
    """Creates interactive maps of data center facilities with generators."""
    
    def __init__(self, data_dir: Path, cache_file: Optional[Path] = None):
        """
        Initialize the mapper.
        
        Args:
            data_dir: Root directory containing extracted data
            cache_file: Optional path to geocoding cache file
        """
        self.data_dir = data_dir
        self.cache_file = cache_file or data_dir.parent / "geocoding_cache.json"
        self.geocoding_cache = self._load_cache()
        
        # Disable SSL verification for geocoding (workaround for corporate networks)
        import urllib3
        urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)
        
        import requests
        from requests.adapters import HTTPAdapter
        from urllib3.util.ssl_ import create_urllib3_context
        
        class SSLAdapter(HTTPAdapter):
            def init_poolmanager(self, *args, **kwargs):
                ctx = create_urllib3_context()
                ctx.check_hostname = False
                ctx.verify_mode = ssl.CERT_NONE
                kwargs['ssl_context'] = ctx
                return super().init_poolmanager(*args, **kwargs)
        
        # Create custom session with SSL disabled
        session = requests.Session()
        session.mount('https://', SSLAdapter())
        session.verify = False
        
        # Create custom adapter for geopy
        from geopy.adapters import RequestsAdapter
        
        class NoSSLAdapter(RequestsAdapter):
            def __init__(self, proxies=None, ssl_context=None):
                super().__init__(proxies=proxies, ssl_context=ssl_context)
                self.session = session
        
        self.geolocator = Nominatim(
            user_agent="permit_facility_mapper", 
            timeout=10,
            adapter_factory=NoSSLAdapter
        )
        
    def _load_cache(self) -> Dict[str, Tuple[float, float]]:
        """Load geocoding cache from file."""
        if self.cache_file.exists():
            try:
                with open(self.cache_file, 'r') as f:
                    return json.load(f)
            except Exception:
                pass
        return {}
    
    def _save_cache(self):
        """Save geocoding cache to file."""
        try:
            self.cache_file.parent.mkdir(parents=True, exist_ok=True)
            with open(self.cache_file, 'w') as f:
                json.dump(self.geocoding_cache, f, indent=2)
        except Exception as e:
            print(f"Warning: Could not save geocoding cache: {e}")
    
    def _clean_address(self, address: str, county: str, state: str = "Virginia") -> str:
        """
        Clean and standardize address for geocoding.
        
        Handles cases where:
        - Multiple addresses are provided
        - Addresses are descriptive rather than specific
        - Address includes county and state info
        - Newlines in addresses
        - Intersection descriptions
        - Address ranges
        - Building/Suite/Attn info
        """
        if not address:
            return f"{county}, {state}"
        
        # Replace newlines with spaces
        address = address.replace('\n', ' ').replace('\r', ' ')
        
        # Normalize whitespace
        address = ' '.join(address.split())
        
        # Handle intersection descriptions - use county instead
        if 'intersection of' in address.lower():
            # For intersections, geocode to the county center
            return f"{county}, {state}"
        
        # Remove common non-address components that confuse geocoding
        # Remove building/suite/floor/attention info
        address = re.sub(r',?\s*(?:Bldg\.?|Building|Suite|Ste\.?|Floor|Fl\.?|Room|Rm\.?)\s+[\w\d\-\.]+', '', address, flags=re.IGNORECASE)
        address = re.sub(r',?\s*Attn:\s*[^,]+', '', address, flags=re.IGNORECASE)
        
        # Handle address ranges like "1300-1700 Street" -> "1300 Street"
        address = re.sub(r'(\d+)\s*-\s*\d+\s+', r'\1 ', address)
        
        # Clean up any double commas or spaces
        address = re.sub(r'\s*,\s*,\s*', ', ', address)
        address = re.sub(r'\s+', ' ', address)
        address = address.strip()
        
        # If multiple addresses separated by commas, take the first one that looks like an address
        parts = [p.strip() for p in address.split(',')]
        
        # Look for street address pattern (numbers at start)
        street_address = None
        for part in parts:
            if re.match(r'^\d+\s+\w+', part):
                street_address = part
                break
        
        if street_address:
            # Found a street address, use it with county and state
            # Check if we already have city in the parts
            city_part = None
            for i, part in enumerate(parts[1:], 1):
                # Look for a part that's not the county and not a state
                if county.replace(' County', '').replace(' county', '') not in part and \
                   not re.match(r'^[A-Z]{2}$', part.strip()) and \
                   not part.strip().isdigit():
                    city_part = part
                    break
            
            if city_part:
                return f"{street_address}, {city_part}, {state}"
            else:
                return f"{street_address}, {county}, {state}"
        else:
            # No clear street address, try using the first part with county
            cleaned = parts[0] if parts else address
            # If it's just descriptive text, use county
            if len(cleaned) < 5 or not any(c.isdigit() for c in cleaned):
                return f"{county}, {state}"
            return f"{cleaned}, {county}, {state}"
    
    def _geocode_address(self, address: str, retry_count: int = 2, skip_rate_limit: bool = False) -> Optional[Tuple[float, float]]:
        """
        Geocode an address with caching and retry logic.
        
        Args:
            address: Address to geocode
            retry_count: Number of retries on failure
            skip_rate_limit: Skip rate limiting sleep (used when called from rate-limited wrapper)
            
        Returns:
            Tuple of (latitude, longitude) or None if geocoding fails
        """
        # Check cache first
        if address in self.geocoding_cache:
            return tuple(self.geocoding_cache[address])
        
        # Try geocoding with retries
        for attempt in range(retry_count):
            try:
                if not skip_rate_limit:
                    time.sleep(1.2)  # Rate limiting (Nominatim allows 1 req/sec)
                location = self.geolocator.geocode(address)
                
                if location:
                    coords = (location.latitude, location.longitude)
                    self.geocoding_cache[address] = coords
                    return coords
                else:
                    return None
                    
            except (GeocoderTimedOut, GeocoderServiceError):
                if attempt < retry_count - 1:
                    time.sleep(2 ** attempt)  # Exponential backoff
                else:
                    return None
            except Exception:
                return None
        
        return None
    
    def _load_single_json(self, json_file: Path, state: str) -> Optional[Dict]:
        """
        Load and parse a single JSON file.
        
        Args:
            json_file: Path to JSON file
            state: State name
            
        Returns:
            Facility dictionary or None if invalid
        """
        try:
            with open(json_file, 'r') as f:
                data = json.load(f)
            
            permit_details = data.get('data', {}).get('permitDetails', {})
            
            # Extract facility information with robust fallbacks
            permit_number = data.get('permit_number') or permit_details.get('permitNumber', 'Unknown')
            facility_name = permit_details.get('facilityName') or 'Unknown Facility'
            facility_address = permit_details.get('facilityAddress') or ''
            facility_county = permit_details.get('facilityCounty') or 'Unknown'
            permit_date = permit_details.get('permitIssuanceDate') or 'Unknown'
            generator_count = data.get('generator_count', 0)
            
            # Calculate total capacity from generator sets
            total_capacity_kw = sum(
                gen.get('ratedCapacityKW', 0) * gen.get('numGenerators', 1)
                for gen in data.get('data', {}).get('generatorSets', [])
                if gen.get('ratedCapacityKW')
            )
            
            # Skip facilities with no meaningful data
            if (not facility_address or facility_address.strip() == '') and \
               (not facility_county or facility_county in ['Unknown', '']):
                return None
            
            # Skip facilities with zero generators
            if generator_count == 0 and total_capacity_kw == 0:
                return None
            
            return {
                'permit_number': permit_number,
                'facility_name': facility_name,
                'address_raw': facility_address,
                'county': facility_county,
                'state': state,
                'generator_count': generator_count,
                'permit_date': permit_date,
                'total_capacity_kw': total_capacity_kw,
                'source_file': data.get('source_file', json_file.name)
            }
            
        except Exception:
            return None
    
    def load_permits(self, states: List[str], limit_per_state: Optional[int] = None) -> pd.DataFrame:
        """
        Load permit data from specified states (optimized for large datasets).
        
        Args:
            states: List of state names to load
            limit_per_state: Optional limit on number of permits per state
            
        Returns:
            DataFrame with facility information
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        
        facilities = []
        
        # Collect all JSON files first
        all_files = []
        for state in states:
            state_dir = self.data_dir / state
            if not state_dir.exists():
                continue
            
            json_files = list(state_dir.glob("*.json"))
            
            # Limit files per state if specified
            if limit_per_state:
                json_files = json_files[:limit_per_state]
            
            all_files.extend([(f, state) for f in json_files])
        
        if not all_files:
            return pd.DataFrame()
        
        # Load files in parallel with progress bar
        print(f"  Loading {len(all_files)} JSON files...")
        
        with ThreadPoolExecutor(max_workers=8) as executor:
            # Submit all loading tasks
            future_to_file = {
                executor.submit(self._load_single_json, json_file, state): (json_file, state)
                for json_file, state in all_files
            }
            
            # Process results as they complete
            for future in tqdm(as_completed(future_to_file), total=len(all_files), desc="  Processing"):
                result = future.result()
                if result is not None:
                    facilities.append(result)
        
        return pd.DataFrame(facilities)
    
    def geocode_facilities(self, df: pd.DataFrame, show_progress: bool = True) -> pd.DataFrame:
        """
        Add geocoded coordinates to facility dataframe (optimized with batch processing).
        
        Args:
            df: DataFrame with facility data
            show_progress: Show progress bar during geocoding
            
        Returns:
            DataFrame with added latitude and longitude columns
        """
        from concurrent.futures import ThreadPoolExecutor, as_completed
        from threading import Lock
        
        # Pre-clean all addresses and check cache
        addresses_to_geocode = []
        coords_list = [None] * len(df)
        
        print("  Preparing addresses...")
        for idx, row in df.iterrows():
            cleaned_address = self._clean_address(
                row['address_raw'], 
                row['county'], 
                row['state']
            )
            
            # Check cache first
            if cleaned_address in self.geocoding_cache:
                coords = tuple(self.geocoding_cache[cleaned_address])
                coords_list[idx] = {
                    'latitude': coords[0],
                    'longitude': coords[1],
                    'geocoded_address': cleaned_address,
                    'geocoding_success': True
                }
            else:
                addresses_to_geocode.append((idx, cleaned_address, row['county'], row['state']))
        
        cached_count = len([c for c in coords_list if c is not None])
        print(f"  Found {cached_count} cached addresses, {len(addresses_to_geocode)} to geocode")
        
        if addresses_to_geocode:
            # Geocode remaining addresses with rate-limited parallelization
            # Use 1 worker to respect Nominatim's 1 req/sec limit
            rate_lock = Lock()
            last_request_time = [0.0]
            
            def geocode_with_rate_limit(idx, address, county, state):
                """Geocode a single address with rate limiting."""
                # Enforce rate limit (1 request per second)
                with rate_lock:
                    elapsed = time.time() - last_request_time[0]
                    if elapsed < 1.2:
                        time.sleep(1.2 - elapsed)
                    last_request_time[0] = time.time()
                
                # Try primary address (skip internal rate limit since we handle it here)
                coords = self._geocode_address(address, skip_rate_limit=True)
                
                if coords:
                    return idx, {
                        'latitude': coords[0],
                        'longitude': coords[1],
                        'geocoded_address': address,
                        'geocoding_success': True
                    }
                
                # Try fallback to county
                fallback_address = f"{county}, {state}"
                coords = self._geocode_address(fallback_address, skip_rate_limit=True)
                
                if coords:
                    return idx, {
                        'latitude': coords[0],
                        'longitude': coords[1],
                        'geocoded_address': fallback_address,
                        'geocoding_success': False
                    }
                
                return idx, {
                    'latitude': None,
                    'longitude': None,
                    'geocoded_address': None,
                    'geocoding_success': False
                }
            
            # Process with single worker (Nominatim rate limit)
            iterator = tqdm(addresses_to_geocode, desc="  Geocoding", disable=not show_progress)
            
            with ThreadPoolExecutor(max_workers=1) as executor:
                future_to_idx = {
                    executor.submit(geocode_with_rate_limit, idx, addr, county, state): idx
                    for idx, addr, county, state in addresses_to_geocode
                }
                
                for future in as_completed(future_to_idx):
                    idx, result = future.result()
                    coords_list[idx] = result
                    if show_progress:
                        iterator.update(1)
        
        # Add coordinates to dataframe
        coords_df = pd.DataFrame(coords_list)
        result_df = pd.concat([df, coords_df], axis=1)
        
        # Save cache after geocoding
        self._save_cache()
        
        return result_df
    
    def create_map(self, df: pd.DataFrame, output_file: Path, title: Optional[str] = None) -> Optional[folium.Map]:
        """
        Create an interactive map with facility markers.
        
        Args:
            df: DataFrame with geocoded facility data
            output_file: Path to save the HTML map
            title: Optional title for the map
            
        Returns:
            Folium map object or None if no valid coordinates
        """
        # Filter out facilities without coordinates or with zero capacity
        df_mapped = df[(df['latitude'].notna()) & (df['total_capacity_kw'] > 0)].copy()
        
        if len(df_mapped) == 0:
            return None
        
        # Calculate map center
        center_lat = df_mapped['latitude'].mean()
        center_lon = df_mapped['longitude'].mean()
        
        # Create base map - Light theme by default (clean, professional)
        m = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=7,
            tiles='CartoDB positron',
            name='Light',
            control_scale=True,
            prefer_canvas=True
        )
        
        # Add alternative map themes
        folium.TileLayer(
            tiles='CartoDB dark_matter',
            name='Dark',
            overlay=False,
            control=True
        ).add_to(m)
        
        folium.TileLayer(
            tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attr='Esri',
            name='Satellite',
            overlay=False,
            control=True
        ).add_to(m)
        
        # Add layer control
        folium.LayerControl(position='topright').add_to(m)
        
        # Add title if provided
        if title:
            title_html = f'''
            <div style="position: fixed; 
                        top: 10px; left: 50px; 
                        width: auto; height: auto;
                        background-color: white; 
                        border:2px solid grey; 
                        z-index:9999; 
                        font-size:16px;
                        padding: 10px;
                        border-radius: 5px;">
                <h3 style="margin: 0;">{title}</h3>
            </div>
            '''
            m.get_root().html.add_child(folium.Element(title_html))
        
        # Add marker cluster for better performance with many markers
        marker_cluster = plugins.MarkerCluster(name='Facilities').add_to(m)
        
        # Color scale based on generator count - using darker, more visible colors
        def get_color(count):
            if count >= 50:
                return 'darkred'
            elif count >= 20:
                return 'darkorange'
            elif count >= 10:
                return 'blue'
            else:
                return 'darkgreen'
        
        # Add markers
        for _, row in df_mapped.iterrows():
            # Create popup content
            popup_html = f"""
            <div style="font-family: Arial; min-width: 250px;">
                <h4 style="margin: 0 0 10px 0; color: #2c3e50;">{row['facility_name']}</h4>
                <table style="width: 100%; font-size: 12px;">
                    <tr><td><b>Permit Number:</b></td><td>{row['permit_number']}</td></tr>
                    <tr><td><b>Generators:</b></td><td>{row['generator_count']}</td></tr>
                    <tr><td><b>Total Capacity:</b></td><td>{row['total_capacity_kw']:,.0f} kW</td></tr>
                    <tr><td><b>County:</b></td><td>{row['county']}</td></tr>
                    <tr><td><b>State:</b></td><td>{row['state']}</td></tr>
                    <tr><td><b>Permit Date:</b></td><td>{row['permit_date']}</td></tr>
                    <tr><td colspan="2" style="padding-top: 8px;"><i>Address: {row['address_raw'][:100]}{"..." if len(row['address_raw']) > 100 else ""}</i></td></tr>
                    <tr><td colspan="2" style="padding-top: 4px; font-size: 10px; color: #7f8c8d;">
                        {"⚠️ Geocoded to county center" if not row['geocoding_success'] else "✓ Precise location"}
                    </td></tr>
                </table>
            </div>
            """
            
            # Create tooltip (hover text)
            tooltip_text = f"{row['facility_name']}<br>{row['generator_count']} generators"
            
            # Add marker
            folium.CircleMarker(
                location=[row['latitude'], row['longitude']],
                radius=8 + (row['generator_count'] / 10),  # Size based on generator count
                popup=folium.Popup(popup_html, max_width=300),
                tooltip=tooltip_text,
                color=get_color(row['generator_count']),
                fill=True,
                fillColor=get_color(row['generator_count']),
                fillOpacity=0.7,
                weight=2
            ).add_to(marker_cluster)
        
        # Add legend with updated colors
        legend_html = """
        <div style="position: fixed; 
                    bottom: 50px; right: 50px; 
                    width: 200px; height: auto; 
                    background-color: white; 
                    border:2px solid grey; 
                    z-index:9999; 
                    font-size:14px;
                    padding: 10px;
                    border-radius: 5px;">
            <h4 style="margin-top: 0;">Generator Count</h4>
            <p style="margin: 5px 0;"><span style="color: darkred;">●</span> 50+ generators</p>
            <p style="margin: 5px 0;"><span style="color: darkorange;">●</span> 20-49 generators</p>
            <p style="margin: 5px 0;"><span style="color: blue;">●</span> 10-19 generators</p>
            <p style="margin: 5px 0;"><span style="color: darkgreen;">●</span> < 10 generators</p>
        </div>
        """
        m.get_root().html.add_child(folium.Element(legend_html))
        
        # Save map
        output_file.parent.mkdir(parents=True, exist_ok=True)
        m.save(str(output_file))
        
        return m
    
    def create_dashboard(self, df: pd.DataFrame, output_file: Path, title: Optional[str] = None) -> None:
        """
        Create an enhanced HTML dashboard with map, statistics, and data table.
        
        Args:
            df: DataFrame with geocoded facility data
            output_file: Path to save the HTML dashboard
            title: Optional title for the dashboard
        """
        # Filter mapped facilities with valid capacity
        df_mapped = df[(df['latitude'].notna()) & (df['total_capacity_kw'] > 0)].copy()
        
        if len(df_mapped) == 0:
            return None
        
        # Calculate statistics
        total_facilities = len(df_mapped)
        total_generators = df_mapped['generator_count'].sum()
        total_capacity_mw = df_mapped['total_capacity_kw'].sum() / 1000
        avg_capacity_mw = total_capacity_mw / total_facilities if total_facilities > 0 else 0
        states = df_mapped['state'].nunique()
        counties = df_mapped['county'].nunique()
        
        # Create map center
        center_lat = df_mapped['latitude'].mean()
        center_lon = df_mapped['longitude'].mean()
        
        # Create professional map - Light theme by default (publication quality)
        map_obj = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=6,
            tiles='CartoDB positron',
            name='Light',
            control_scale=True,
            prefer_canvas=True
        )
        
        # Add alternative map themes
        folium.TileLayer(
            tiles='CartoDB dark_matter',
            name='Dark',
            overlay=False,
            control=True
        ).add_to(map_obj)
        
        folium.TileLayer(
            tiles='https://server.arcgisonline.com/ArcGIS/rest/services/World_Imagery/MapServer/tile/{z}/{y}/{x}',
            attr='Esri',
            name='Satellite',
            overlay=False,
            control=True
        ).add_to(map_obj)
        
        # Add markers - clean, professional styling with color scheme based on CAPACITY
        for _, row in df_mapped.iterrows():
            # Professional color scheme based on total capacity (MW)
            capacity_mw = row['total_capacity_kw'] / 1000
            
            # Color thresholds based on capacity
            if capacity_mw >= 50:  # 50+ MW
                base_color = '#d32f2f'  # Red - critical
                border_color = '#b71c1c'
                category = 'Critical (50+ MW)'
            elif capacity_mw >= 20:  # 20-50 MW
                base_color = '#f57c00'  # Orange - high
                border_color = '#e65100'
                category = 'High (20-50 MW)'
            elif capacity_mw >= 10:  # 10-20 MW
                base_color = '#0288d1'  # Blue - medium
                border_color = '#01579b'
                category = 'Medium (10-20 MW)'
            else:  # < 10 MW
                base_color = '#388e3c'  # Green - low
                border_color = '#1b5e20'
                category = 'Low (<10 MW)'
            
            popup_html = f"""
            <div style="font-family: 'Helvetica Neue', Arial, sans-serif; min-width: 280px; font-size: 13px;">
                <div style="border-bottom: 3px solid {base_color}; padding-bottom: 8px; margin-bottom: 10px;">
                    <div style="font-weight: 600; font-size: 14px; color: #212121;">{row['facility_name']}</div>
                    <div style="color: #757575; font-size: 12px; margin-top: 2px;">Permit {row['permit_number']}</div>
                </div>
                <table style="width: 100%; border-collapse: collapse; font-size: 13px; line-height: 1.8;">
                    <tr>
                        <td style="color: #616161; padding: 4px 0;">Generators:</td>
                        <td style="text-align: right; font-weight: 600; color: {border_color};">{row['generator_count']}</td>
                    </tr>
                    <tr>
                        <td style="color: #616161; padding: 4px 0;">Capacity:</td>
                        <td style="text-align: right; font-weight: 600;">{row['total_capacity_kw']/1000:.0f} MW</td>
                    </tr>
                    <tr style="border-top: 1px solid #e0e0e0;">
                        <td style="color: #616161; padding: 8px 0 4px 0;">County:</td>
                        <td style="text-align: right;">{row['county']}</td>
                    </tr>
                    <tr>
                        <td style="color: #616161; padding: 4px 0;">State:</td>
                        <td style="text-align: right; font-weight: 500;">{row['state']}</td>
                    </tr>
                    <tr>
                        <td style="color: #616161; padding: 4px 0;">Permit Date:</td>
                        <td style="text-align: right;">{row['permit_date']}</td>
                    </tr>
                </table>
            </div>
            """
            
            # Tooltip showing capacity prominently
            tooltip_text = f"{row['facility_name']}: {capacity_mw:.1f} MW ({row['generator_count']} generators)"
            
            # Calculate marker size based on CAPACITY (MW) - more meaningful scale
            # Using log scale for better visual distribution
            base_radius = 6
            if capacity_mw > 0:
                # Log scale: radius grows logarithmically with capacity
                size_multiplier = min(2.5 * (capacity_mw ** 0.5), 20)  # Square root scale, capped at 20
            else:
                size_multiplier = 0
            marker_radius = base_radius + size_multiplier
            
            # Clean circle markers
            folium.CircleMarker(
                location=[row['latitude'], row['longitude']],
                radius=marker_radius,
                popup=folium.Popup(popup_html, max_width=320),
                tooltip=tooltip_text,
                color=border_color,
                fill=True,
                fillColor=base_color,
                fillOpacity=0.75,
                weight=2,
                opacity=1.0
            ).add_to(map_obj)
        
        folium.LayerControl(position='topright').add_to(map_obj)
        
        # Get map HTML
        map_html = map_obj._repr_html_()
        
        # Build data table rows - SORTED BY CAPACITY
        table_rows = ""
        for _, row in df_mapped.sort_values('total_capacity_kw', ascending=False).iterrows():
            # Use the same color scheme for consistency (based on capacity)
            capacity_mw = row['total_capacity_kw'] / 1000
            if capacity_mw >= 50:
                badge_color = '#d32f2f'
                border_color = '#b71c1c'
            elif capacity_mw >= 20:
                badge_color = '#f57c00'
                border_color = '#e65100'
            elif capacity_mw >= 10:
                badge_color = '#0288d1'
                border_color = '#01579b'
            else:
                badge_color = '#388e3c'
                border_color = '#1b5e20'
            
            table_rows += f"""
            <tr>
                <td><b>{row['facility_name']}</b></td>
                <td>{row['permit_number']}</td>
                <td>{row['generator_count']}</td>
                <td><span class="badge" style="background-color: {badge_color}; border: 2px solid {border_color};">{capacity_mw:.0f} MW</span></td>
                <td>{row['county']}</td>
                <td>{row['state']}</td>
                <td>{row['permit_date']}</td>
            </tr>
            """
        
        # State-by-state breakdown
        state_stats = df_mapped.groupby('state').agg({
            'facility_name': 'count',
            'generator_count': 'sum',
            'total_capacity_kw': 'sum'
        }).reset_index()
        state_stats.columns = ['State', 'Facilities', 'Generators', 'Capacity (kW)']
        state_stats['Capacity (MW)'] = state_stats['Capacity (kW)'] / 1000
        
        state_rows = ""
        for _, row in state_stats.iterrows():
            state_rows += f"""
            <tr>
                <td><b>{row['State']}</b></td>
                <td>{row['Facilities']}</td>
                <td>{row['Generators']}</td>
                <td>{row['Capacity (MW)']:.1f} MW</td>
            </tr>
            """
        
        # Create professional, publication-ready dashboard HTML with text-based NREL branding
        dashboard_html = f"""
<!DOCTYPE html>
<html lang="en">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{title or 'Data Center Facilities'}</title>
    <style>
        * {{
            margin: 0;
            padding: 0;
            box-sizing: border-box;
        }}
        
        body {{
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', 'Helvetica Neue', Arial, sans-serif;
            background: #ffffff;
            color: #212121;
            line-height: 1.5;
        }}
        
        .container {{
            max-width: 1400px;
            margin: 0 auto;
            padding: 20px;
        }}
        
        .header {{
            border-bottom: 3px solid #e0e0e0;
            padding-bottom: 20px;
            margin-bottom: 30px;
            display: flex;
            justify-content: space-between;
            align-items: center;
        }}
        
        .header-content {{
            flex: 1;
        }}
        
        .header-logo {{
            flex-shrink: 0;
            margin-left: 20px;
        }}
        
        .nrel-text {{
            font-size: 60px;
            font-weight: 1000;
            color: #0079C2;
            letter-spacing: 2px;
            font-family: Arial, Helvetica, sans-serif;
        }}
        
        .header h1 {{
            font-size: 28px;
            font-weight: 600;
            color: #212121;
            margin-bottom: 8px;
        }}
        
        .header p {{
            font-size: 14px;
            color: #616161;
        }}
        
        .stats {{
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
            gap: 16px;
            margin-bottom: 30px;
        }}
        
        .stat-box {{
            background: #fafafa;
            border: 1px solid #e0e0e0;
            border-radius: 4px;
            padding: 16px;
            text-align: center;
        }}
        
        .stat-value {{
            font-size: 32px;
            font-weight: 700;
            color: #212121;
            margin-bottom: 4px;
        }}
        
        .stat-label {{
            font-size: 12px;
            color: #757575;
            text-transform: uppercase;
            letter-spacing: 0.5px;
            font-weight: 500;
        }}
        
        .map-section {{
            margin-bottom: 30px;
            display: grid;
            grid-template-columns: 1fr auto;
            gap: 20px;
        }}
        
        .map-container {{
            background: white;
            border: 1px solid #e0e0e0;
            border-radius: 4px;
            overflow: hidden;
            height: 600px;
        }}
        
        .legend {{
            background: white;
            border: 1px solid #e0e0e0;
            border-radius: 4px;
            padding: 20px;
            width: 220px;
            height: fit-content;
        }}
        
        .legend h3 {{
            font-size: 14px;
            font-weight: 600;
            color: #212121;
            margin-bottom: 16px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        
        .legend-item {{
            display: flex;
            align-items: center;
            margin-bottom: 12px;
            font-size: 13px;
        }}
        
        .legend-dot {{
            width: 14px;
            height: 14px;
            border-radius: 50%;
            margin-right: 10px;
            flex-shrink: 0;
        }}
        
        .section {{
            background: white;
            border: 1px solid #e0e0e0;
            border-radius: 4px;
            padding: 24px;
            margin-bottom: 30px;
        }}
        
        .section-title {{
            font-size: 16px;
            font-weight: 600;
            color: #212121;
            margin-bottom: 20px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        
        table {{
            width: 100%;
            border-collapse: collapse;
            font-size: 13px;
        }}
        
        th {{
            background: #fafafa;
            padding: 12px;
            text-align: left;
            font-weight: 600;
            color: #424242;
            border-bottom: 2px solid #e0e0e0;
            font-size: 12px;
            text-transform: uppercase;
            letter-spacing: 0.5px;
        }}
        
        td {{
            padding: 12px;
            border-bottom: 1px solid #f5f5f5;
            color: #424242;
        }}
        
        tr:hover {{
            background: #fafafa;
        }}
        
        .badge {{
            display: inline-block;
            padding: 4px 10px;
            border-radius: 3px;
            color: white;
            font-weight: 600;
            font-size: 12px;
        }}
        
        .footer {{
            text-align: center;
            padding: 24px;
            color: #9e9e9e;
            font-size: 12px;
            border-top: 1px solid #e0e0e0;
            margin-top: 40px;
        }}
        
        @media (max-width: 1024px) {{
            .map-section {{
                grid-template-columns: 1fr;
            }}
            .legend {{
                width: 100%;
            }}
            .map-container {{
                height: 500px;
            }}
        }}
    </style>
</head>
<body>
    <div class="container">
        <div class="header">
            <div class="header-content">
                <h1>{title or 'Data Center Facilities'}</h1>
                <p>Air quality permit data for backup generator facilities</p>
            </div>
            <div class="header-logo">
                <div class="nrel-text">NREL</div>
            </div>
        </div>
        
        <div class="stats">
            <div class="stat-box">
                <div class="stat-value">{total_facilities}</div>
                <div class="stat-label">Facilities</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{total_generators}</div>
                <div class="stat-label">Generators</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{total_capacity_mw:.1f}</div>
                <div class="stat-label">Total MW</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{avg_capacity_mw:.1f}</div>
                <div class="stat-label">Avg MW/Facility</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{states}</div>
                <div class="stat-label">States</div>
            </div>
            <div class="stat-box">
                <div class="stat-value">{counties}</div>
                <div class="stat-label">Counties</div>
            </div>
        </div>
        
        <div class="map-section">
            <div class="map-container">
                {map_html}
            </div>
            
            <div class="legend">
                <h3>Total Capacity (MW)</h3>
                <div class="legend-item">
                    <div class="legend-dot" style="background: #d32f2f; border: 2px solid #b71c1c;"></div>
                    <span>50+ MW (Critical)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-dot" style="background: #f57c00; border: 2px solid #e65100;"></div>
                    <span>20-50 MW (High)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-dot" style="background: #0288d1; border: 2px solid #01579b;"></div>
                    <span>10-20 MW (Medium)</span>
                </div>
                <div class="legend-item">
                    <div class="legend-dot" style="background: #388e3c; border: 2px solid #1b5e20;"></div>
                    <span>&lt;10 MW (Low)</span>
                </div>
                <div style="margin-top: 16px; padding-top: 16px; border-top: 1px solid #e0e0e0; font-size: 11px; color: #757575;">
                    <b>Note:</b> Circle size represents total facility capacity
                </div>
            </div>
        </div>
        
        <div class="section">
            <h2 class="section-title">Facility Details</h2>
            <table>
                <thead>
                    <tr>
                        <th>Facility Name</th>
                        <th>Permit</th>
                        <th>Generators</th>
                        <th>Capacity</th>
                        <th>County</th>
                        <th>State</th>
                        <th>Permit Date</th>
                    </tr>
                </thead>
                <tbody>
                    {table_rows}
                </tbody>
            </table>
        </div>
        
        <div class="section">
            <h2 class="section-title">Summary by State</h2>
            <table>
                <thead>
                    <tr>
                        <th>State</th>
                        <th>Facilities</th>
                        <th>Generators</th>
                        <th>Total Capacity</th>
                    </tr>
                </thead>
                <tbody>
                    {state_rows}
                </tbody>
            </table>
        </div>
        
        <div class="footer">
            <p>Data extracted from air quality permits using automated LLM extraction</p>
            <p>Geocoding powered by OpenStreetMap Nominatim</p>
        </div>
    </div>
</body>
</html>
"""
        
        # Save dashboard
        output_file.parent.mkdir(parents=True, exist_ok=True)
        # Save dashboard
        output_file.parent.mkdir(parents=True, exist_ok=True)
        with open(output_file, 'w', encoding='utf-8') as f:
            f.write(dashboard_html)
        
        return output_file
    
    def generate_summary_stats(self, df: pd.DataFrame) -> Dict:
        """Generate summary statistics."""
        df_valid = df[df['latitude'].notna()].copy()
        
        summary = {
            'total_facilities': len(df),
            'facilities_mapped': len(df_valid),
            'total_generators': int(df_valid['generator_count'].sum()),
            'total_capacity_mw': float(df_valid['total_capacity_kw'].sum() / 1000),
            'avg_generators_per_facility': float(df_valid['generator_count'].mean()),
            'states': int(df['state'].nunique()),
            'counties': int(df['county'].nunique()),
            'geocoding_success_rate': float(len(df_valid) / len(df) * 100) if len(df) > 0 else 0
        }
        
        # State-level summary
        state_summary = df_valid.groupby('state').agg({
            'facility_name': 'count',
            'generator_count': 'sum',
            'total_capacity_kw': 'sum'
        }).rename(columns={
            'facility_name': 'facilities',
            'generator_count': 'total_generators',
            'total_capacity_kw': 'total_capacity_mw'
        })
        state_summary['total_capacity_mw'] = state_summary['total_capacity_mw'] / 1000
        
        summary['by_state'] = state_summary.to_dict('index')
        
        return summary
