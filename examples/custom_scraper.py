"""
Example: Custom scraper for a new state

This example shows how to create a scraper for a new state
by extending the BaseScraper class.
"""

from pathlib import Path
from typing import List, Dict, Any
import requests
from bs4 import BeautifulSoup

from permit_toolkit.scrapers.base import BaseScraper


class CustomStateScraper(BaseScraper):
    """
    Template scraper for adding a new state.
    
    Customize the methods below for your state's specific website structure.
    """
    
    BASE_URL = "https://example-state-deq.gov"
    PERMITS_URL = f"{BASE_URL}/air-quality/permits"
    
    def get_permit_list(self) -> List[Dict[str, Any]]:
        """
        Get list of permits to download.
        
        Customize this method to scrape your state's permit list.
        Should return a list of dictionaries with permit metadata.
        """
        permits = []
        
        try:
            # Example: Scrape permits list from a webpage
            response = requests.get(self.PERMITS_URL)
            soup = BeautifulSoup(response.content, 'html.parser')
            
            # Example: Find permit links (customize selectors for your state)
            for link in soup.find_all('a', class_='permit-link'):
                permit_info = {
                    'id': link.get('data-permit-id'),
                    'facility_name': link.text.strip(),
                    'url': self.BASE_URL + link.get('href'),
                    'permit_number': link.get('data-permit-id'),
                }
                permits.append(permit_info)
            
        except Exception as e:
            self.logger.error(f"Error fetching permit list: {e}")
        
        return permits
    
    def download_permit(self, permit_info: Dict[str, Any], output_path: Path) -> bool:
        """
        Download a single permit.
        
        Customize this method for your state's download mechanism.
        """
        try:
            # Example: Direct PDF download
            response = requests.get(permit_info['url'], stream=True)
            response.raise_for_status()
            
            with open(output_path, 'wb') as f:
                for chunk in response.iter_content(chunk_size=8192):
                    f.write(chunk)
            
            self.logger.info(f"✓ Downloaded: {output_path.name}")
            return True
            
        except Exception as e:
            self.logger.error(f"✗ Error downloading {permit_info['id']}: {e}")
            return False
    
    def _get_output_path(self, permit_info: Dict[str, Any]) -> Path:
        """
        Generate output file path for a permit.
        
        Customize filename format as needed.
        """
        permit_number = permit_info['permit_number']
        filename = f"{permit_number}_Permit.pdf"
        return self.output_dir / filename


def main():
    """Example usage of custom scraper."""
    scraper = CustomStateScraper(
        output_dir=Path("data/permits/CustomState"),
        test_mode=True,
        test_count=5,
        resume=True
    )
    
    # Run the scraper
    # scraper.run()  # Uncomment to execute
    print("Custom scraper configured. Uncomment scraper.run() to execute.")


if __name__ == "__main__":
    main()
