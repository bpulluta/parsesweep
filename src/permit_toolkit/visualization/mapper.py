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
        """
        if not address:
            return f"{county}, {state}"
        
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
            return f"{street_address}, {county}, {state}"
        else:
            # No clear street address, try using the first part with county
            cleaned = parts[0] if parts else address
            return f"{cleaned}, {county}, {state}"
    
    def _geocode_address(self, address: str, retry_count: int = 2) -> Optional[Tuple[float, float]]:
        """
        Geocode an address with caching and retry logic.
        
        Args:
            address: Address to geocode
            retry_count: Number of retries on failure
            
        Returns:
            Tuple of (latitude, longitude) or None if geocoding fails
        """
        # Check cache first
        if address in self.geocoding_cache:
            return tuple(self.geocoding_cache[address])
        
        # Try geocoding with retries
        for attempt in range(retry_count):
            try:
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
    
    def load_permits(self, states: List[str], limit_per_state: Optional[int] = None) -> pd.DataFrame:
        """
        Load permit data from specified states.
        
        Args:
            states: List of state names to load
            limit_per_state: Optional limit on number of permits per state
            
        Returns:
            DataFrame with facility information
        """
        facilities = []
        
        for state in states:
            state_dir = self.data_dir / state
            if not state_dir.exists():
                continue
            
            # Match both Virginia format (*_DC_Permit.json) and Illinois format (*.json)
            json_files = list(state_dir.glob("*.json"))
            
            # Limit files per state if specified
            if limit_per_state:
                json_files = json_files[:limit_per_state]
            
            for json_file in json_files:
                try:
                    with open(json_file, 'r') as f:
                        data = json.load(f)
                    
                    permit_details = data.get('data', {}).get('permitDetails', {})
                    
                    facility = {
                        'permit_number': data.get('permit_number', 'Unknown'),
                        'facility_name': permit_details.get('facilityName', 'Unknown'),
                        'address_raw': permit_details.get('facilityAddress', ''),
                        'county': permit_details.get('facilityCounty', 'Unknown'),
                        'state': state,
                        'generator_count': data.get('generator_count', 0),
                        'permit_date': permit_details.get('permitIssuanceDate', 'Unknown'),
                        'total_capacity_kw': sum(
                            gen.get('ratedCapacityKW', 0) * gen.get('numGenerators', 1)
                            for gen in data.get('data', {}).get('generatorSets', [])
                            if gen.get('ratedCapacityKW')
                        ),
                        'source_file': data.get('source_file', json_file.name)
                    }
                    
                    facilities.append(facility)
                    
                except Exception:
                    continue
        
        return pd.DataFrame(facilities)
    
    def geocode_facilities(self, df: pd.DataFrame, show_progress: bool = True) -> pd.DataFrame:
        """
        Add geocoded coordinates to facility dataframe.
        
        Args:
            df: DataFrame with facility data
            show_progress: Show progress bar during geocoding
            
        Returns:
            DataFrame with added latitude and longitude columns
        """
        coords_list = []
        iterator = tqdm(df.iterrows(), total=len(df), desc="Geocoding") if show_progress else df.iterrows()
        
        for idx, row in iterator:
            cleaned_address = self._clean_address(
                row['address_raw'], 
                row['county'], 
                row['state']
            )
            
            coords = self._geocode_address(cleaned_address)
            
            if coords:
                coords_list.append({
                    'latitude': coords[0],
                    'longitude': coords[1],
                    'geocoded_address': cleaned_address,
                    'geocoding_success': True
                })
            else:
                # Try fallback to just county and state
                fallback_address = f"{row['county']}, {row['state']}"
                coords = self._geocode_address(fallback_address)
                
                if coords:
                    coords_list.append({
                        'latitude': coords[0],
                        'longitude': coords[1],
                        'geocoded_address': fallback_address,
                        'geocoding_success': False  # Fallback used
                    })
                else:
                    coords_list.append({
                        'latitude': None,
                        'longitude': None,
                        'geocoded_address': None,
                        'geocoding_success': False
                    })
        
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
        # Filter out facilities without coordinates
        df_mapped = df[df['latitude'].notna()].copy()
        
        if len(df_mapped) == 0:
            return None
        
        # Calculate map center
        center_lat = df_mapped['latitude'].mean()
        center_lon = df_mapped['longitude'].mean()
        
        # Create base map
        m = folium.Map(
            location=[center_lat, center_lon],
            zoom_start=7,
            tiles='OpenStreetMap'
        )
        
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
        
        # Add layer control
        folium.LayerControl().add_to(m)
        
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
