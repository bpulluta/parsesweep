"""
Uptime Institute Awards Scraper

Scrapes award achievements data from Uptime Institute website and exports to CSV.
Implements the BaseScraper interface for consistency with other scrapers.
"""

import logging
import time
import csv
from pathlib import Path
from datetime import datetime
from typing import List, Dict, Any

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

from streamline_extract.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class UptimeInstituteScraper(BaseScraper):
    """
    Scraper for Uptime Institute Awards achievements.
    
    Features:
    - Scrapes all pages of award data (default: 11 pages)
    - Extracts facility names, locations, award types, tiers, etc.
    - Exports to CSV format
    - Resume capability (skips if output CSV exists)
    """
    
    BASE_URL = "https://uptimeinstitute.com/uptime-institute-awards/achievements"
    
    def __init__(
        self,
        output_dir: Path,
        num_pages: int = 11,
        test_mode: bool = False,
        test_count: int = 5,
        resume: bool = True,
    ):
        """
        Initialize Uptime Institute scraper.
        
        Args:
            output_dir: Directory to save CSV output
            num_pages: Number of pages to scrape
            test_mode: If True, only process test_count pages
            test_count: Number of pages to process in test mode
            resume: If True, skip if output file exists
        """
        super().__init__(output_dir, test_mode, test_count, resume)
        self.num_pages = num_pages
        self.driver = None
        self.all_awards = []
        
    def _setup_driver(self) -> webdriver.Chrome:
        """Setup Chrome driver for web scraping."""
        chrome_options = Options()
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--ignore-certificate-errors")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        # Uncomment for headless mode
        # chrome_options.add_argument("--headless")
        
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(60)
        
        return driver
    
    def get_permit_list(self) -> List[Dict[str, Any]]:
        """
        Get list of all awards across all pages.
        
        Returns:
            List of award metadata dictionaries (one per page to process)
        """
        # Return page numbers to process - actual scraping happens in "download"
        pages_to_process = list(range(1, self.num_pages + 1))
        
        if self.test_mode:
            pages_to_process = pages_to_process[:self.test_count]
        
        return [{"page": page, "id": f"page_{page}"} for page in pages_to_process]
    
    def _scrape_page(self, page_num: int) -> List[Dict[str, Any]]:
        """
        Scrape awards data from a single page.
        
        Args:
            page_num: Page number to scrape
            
        Returns:
            List of award dictionaries
        """
        logger.info(f"  Scraping page {page_num}...")
        
        try:
            # Navigate to page
            if page_num == 1:
                url = self.BASE_URL
            else:
                url = f"{self.BASE_URL}?page={page_num}"
            
            self.driver.get(url)
            
            # Wait for content to load
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "body"))
            )
            time.sleep(3)  # Additional wait for dynamic content
            
            awards = []
            
            # Try multiple selectors to find award entries
            # Pattern 1: Look for table structure
            try:
                table = self.driver.find_element(By.CSS_SELECTOR, "table.achievements, table[class*='award']")
                rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
                
                for row in rows:
                    cells = row.find_elements(By.TAG_NAME, "td")
                    if len(cells) >= 2:
                        award = self._extract_from_table_row(cells)
                        if award:
                            awards.append(award)
                            
            except Exception as e:
                logger.debug(f"No table found: {e}")
            
            # Pattern 2: Look for article/card structure
            if not awards:
                try:
                    award_items = self.driver.find_elements(
                        By.CSS_SELECTOR, 
                        "article.achievement, div.achievement, div[class*='award-card']"
                    )
                    
                    for item in award_items:
                        award = self._extract_from_card(item)
                        if award:
                            awards.append(award)
                            
                except Exception as e:
                    logger.debug(f"No cards found: {e}")
            
            # Pattern 3: Generic list items
            if not awards:
                try:
                    award_items = self.driver.find_elements(
                        By.CSS_SELECTOR,
                        "li.achievement-item, div.achievement-item"
                    )
                    
                    for item in award_items:
                        award = self._extract_generic(item)
                        if award:
                            awards.append(award)
                            
                except Exception as e:
                    logger.debug(f"No list items found: {e}")
            
            logger.info(f"  Found {len(awards)} awards on page {page_num}")
            return awards
            
        except Exception as e:
            logger.error(f"  Error scraping page {page_num}: {e}")
            return []
    
    def _extract_from_table_row(self, cells) -> Dict[str, Any]:
        """Extract award data from table row cells."""
        try:
            award = {
                'facility_name': cells[0].text.strip() if len(cells) > 0 else '',
                'location': cells[1].text.strip() if len(cells) > 1 else '',
                'company': cells[2].text.strip() if len(cells) > 2 else '',
                'award_type': cells[3].text.strip() if len(cells) > 3 else '',
                'tier': cells[4].text.strip() if len(cells) > 4 else '',
                'year': cells[5].text.strip() if len(cells) > 5 else '',
            }
            
            # Only return if we have at least a facility name
            if award['facility_name']:
                return award
                
        except Exception as e:
            logger.debug(f"Error extracting from table row: {e}")
        
        return None
    
    def _extract_from_card(self, element) -> Dict[str, Any]:
        """Extract award data from card/article element."""
        try:
            award = {
                'facility_name': '',
                'location': '',
                'company': '',
                'award_type': '',
                'tier': '',
                'year': '',
            }
            
            # Try to find facility name
            name_selectors = [
                "h2.facility-name, h3.facility-name, h4.facility-name",
                "h2, h3, h4",
                ".facility-name, .name, .title"
            ]
            
            for selector in name_selectors:
                try:
                    name_elem = element.find_element(By.CSS_SELECTOR, selector)
                    award['facility_name'] = name_elem.text.strip()
                    if award['facility_name']:
                        break
                except:
                    continue
            
            # Try to find location
            location_selectors = [
                ".location", ".city", ".address",
                "*[class*='location']", "*[class*='city']"
            ]
            
            for selector in location_selectors:
                try:
                    loc_elem = element.find_element(By.CSS_SELECTOR, selector)
                    award['location'] = loc_elem.text.strip()
                    if award['location']:
                        break
                except:
                    continue
            
            # Try to find company
            company_selectors = [".company", ".owner", ".operator"]
            for selector in company_selectors:
                try:
                    comp_elem = element.find_element(By.CSS_SELECTOR, selector)
                    award['company'] = comp_elem.text.strip()
                    if award['company']:
                        break
                except:
                    continue
            
            # Try to find award type/tier
            award_selectors = [".award-type", ".tier", ".certification"]
            for selector in award_selectors:
                try:
                    award_elem = element.find_element(By.CSS_SELECTOR, selector)
                    text = award_elem.text.strip()
                    if 'tier' in text.lower():
                        award['tier'] = text
                    else:
                        award['award_type'] = text
                except:
                    continue
            
            # Try to find year
            year_selectors = [".year", ".date", "*[class*='year']"]
            for selector in year_selectors:
                try:
                    year_elem = element.find_element(By.CSS_SELECTOR, selector)
                    award['year'] = year_elem.text.strip()
                    if award['year']:
                        break
                except:
                    continue
            
            # Return if we found at least a facility name
            if award['facility_name']:
                return award
                
        except Exception as e:
            logger.debug(f"Error extracting from card: {e}")
        
        return None
    
    def _extract_generic(self, element) -> Dict[str, Any]:
        """Extract award data using generic text parsing."""
        try:
            # Get all text from the element
            text = element.text.strip()
            if not text:
                return None
            
            lines = [line.strip() for line in text.split('\n') if line.strip()]
            
            award = {
                'facility_name': lines[0] if len(lines) > 0 else '',
                'location': lines[1] if len(lines) > 1 else '',
                'company': lines[2] if len(lines) > 2 else '',
                'award_type': lines[3] if len(lines) > 3 else '',
                'tier': '',
                'year': '',
            }
            
            # Try to extract year from any line
            for line in lines:
                if any(char.isdigit() for char in line) and '20' in line:
                    # Likely contains a year
                    import re
                    year_match = re.search(r'20\d{2}', line)
                    if year_match:
                        award['year'] = year_match.group()
                        break
            
            return award if award['facility_name'] else None
            
        except Exception as e:
            logger.debug(f"Error extracting generic: {e}")
        
        return None
    
    def download_permit(self, permit_info: Dict[str, Any], output_path: Path) -> bool:
        """
        Scrape a single page (treats each page as a 'permit' for base class compatibility).
        
        Args:
            permit_info: Page metadata dictionary
            output_path: Not used for this scraper (we write all data at end)
            
        Returns:
            True if successful, False otherwise
        """
        page_num = permit_info['page']
        
        if self.driver is None:
            self.driver = self._setup_driver()
        
        awards = self._scrape_page(page_num)
        self.all_awards.extend(awards)
        
        # Add page number to each award for tracking
        for award in awards:
            award['source_page'] = page_num
        
        return len(awards) > 0
    
    def _get_output_path(self, permit_info: Dict[str, Any]) -> Path:
        """Generate output file path (not actually used for CSV output)."""
        return self.output_dir / f"page_{permit_info['page']}.tmp"
    
    def save_to_csv(self, output_file: Path):
        """
        Export all collected awards to CSV file.
        
        Args:
            output_file: Path to output CSV file
        """
        if not self.all_awards:
            logger.warning("No awards data to export!")
            return
        
        # Get all unique keys
        fieldnames = set()
        for award in self.all_awards:
            fieldnames.update(award.keys())
        fieldnames = sorted(list(fieldnames))
        
        logger.info(f"\nExporting {len(self.all_awards)} awards to {output_file}...")
        
        with open(output_file, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            writer.writerows(self.all_awards)
        
        logger.info(f"✓ Successfully exported to {output_file}")
    
    def run(self):
        """Execute the scraping process and export to CSV."""
        try:
            # Check if output exists and we should resume
            output_csv = self.output_dir / "uptime_awards_achievements.csv"
            if self.resume and output_csv.exists():
                logger.info(f"Output file already exists: {output_csv}")
                logger.info("Use resume=False to re-scrape")
                return
            
            # Run the base scraping logic (calls download_permit for each page)
            super().run()
            
            # Export all collected awards to CSV
            self.save_to_csv(output_csv)
            
            # Print summary
            logger.info("\n" + "="*50)
            logger.info("SCRAPING SUMMARY")
            logger.info("="*50)
            logger.info(f"Pages scraped:      {self.stats['downloaded']}")
            logger.info(f"Total awards:       {len(self.all_awards)}")
            logger.info(f"Output file:        {output_csv}")
            logger.info("="*50)
            
        finally:
            if self.driver:
                self.driver.quit()
                logger.info("Browser closed")
