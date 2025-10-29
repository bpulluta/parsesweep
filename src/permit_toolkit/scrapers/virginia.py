"""
Virginia DEQ Air Quality Permit Scraper

Downloads air quality permits for data centers from Virginia DEQ website.
Implements the BaseScraper interface for consistency with other state scrapers.
"""

import logging
import time
from pathlib import Path
from datetime import datetime
from collections import defaultdict
from typing import List, Dict, Any

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options

from permit_toolkit.scrapers.base import BaseScraper

logger = logging.getLogger(__name__)


class VirginiaScraper(BaseScraper):
    """
    Scraper for Virginia DEQ data center air quality permits.
    
    Features:
    - Downloads all permit versions with smart naming
    - Distinguishes between latest and historical versions
    - Resume capability (skips existing files)
    - Generates download manifest with metadata
    
    File Naming Convention:
        LATEST:     {reg_no}_DC_Permit.pdf
        HISTORICAL: {reg_no}_v{YYYYMMDD}_DC_Permit.pdf
    """
    
    BASE_URL = "https://www.deq.virginia.gov"
    PERMITS_URL = (
        f"{BASE_URL}/news-info/shortcuts/permits/air/"
        "issued-air-permits-for-data-centers"
    )
    
    def __init__(
        self,
        output_dir: Path,
        test_mode: bool = False,
        test_count: int = 5,
        latest_only: bool = False,
        resume: bool = True,
    ):
        """
        Initialize Virginia DEQ scraper.
        
        Args:
            output_dir: Directory to save downloaded permits
            test_mode: If True, only process test_count permits
            test_count: Number of permits to process in test mode
            latest_only: If True, only download latest version of each permit
            resume: If True, skip existing files
        """
        super().__init__(output_dir, test_mode, test_count, resume)
        self.latest_only = latest_only
        self.driver = None
        
    def _setup_driver(self) -> webdriver.Chrome:
        """Setup Chrome driver with auto-download preferences."""
        chrome_options = Options()
        
        # Set download preferences
        prefs = {
            "download.default_directory": str(self.output_dir),
            "download.prompt_for_download": False,
            "download.directory_upgrade": True,
            "safebrowsing.enabled": False,
            "profile.default_content_settings.popups": 0,
            "profile.default_content_setting_values.automatic_downloads": 1,
            "plugins.always_open_pdf_externally": True,
            "plugins.plugins_disabled": ["Chrome PDF Viewer"],
            "download_bubble.partial_view_enabled": False,
            "pdfjs.disabled": True,
        }
        chrome_options.add_experimental_option("prefs", prefs)
        chrome_options.add_argument("--disable-blink-features=AutomationControlled")
        chrome_options.add_argument("--ignore-certificate-errors")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(30)
        
        # Set download behavior
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": str(self.output_dir)
        })
        
        return driver
    
    def get_permit_list(self) -> List[Dict[str, Any]]:
        """
        Extract permit list from Virginia DEQ website.
        
        Returns:
            List of permit metadata dictionaries
        """
        self.driver = self._setup_driver()
        
        try:
            logger.info(f"Loading: {self.PERMITS_URL}")
            self.driver.get(self.PERMITS_URL)
            
            # Wait for table
            WebDriverWait(self.driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "table"))
            )
            time.sleep(2)
            
            # Change to "All Rows" view
            try:
                pagesize_select = self.driver.find_element(By.CSS_SELECTOR, "select.pagesize")
                pagesize_select.click()
                time.sleep(0.5)
                all_rows_option = self.driver.find_element(
                    By.XPATH, "//select[@class='pagesize']/option[text()='All Rows']"
                )
                all_rows_option.click()
                time.sleep(2)
                logger.info("Changed page size to 'All Rows'")
            except Exception as e:
                logger.warning(f"Could not change page size: {e}")
            
            # Extract permit data
            permits_data = self._extract_permits_from_table()
            
            # Group by registration number and mark versions
            permits_by_reg_no = defaultdict(list)
            for permit in permits_data:
                permits_by_reg_no[permit['registration_no']].append(permit)
            
            # Sort and mark versions
            for reg_no, permit_list in permits_by_reg_no.items():
                permit_list.sort(key=lambda x: x['date_sortable'], reverse=True)
                for i, permit in enumerate(permit_list):
                    permit['version_priority'] = i + 1
                    permit['is_latest'] = (i == 0)
                    
                    # Generate filename
                    if permit['is_latest']:
                        permit['filename'] = f"{reg_no}_DC_Permit.pdf"
                    else:
                        permit['filename'] = f"{reg_no}_v{permit['date_compact']}_DC_Permit.pdf"
            
            logger.info(f"Found {len(permits_data)} total permit entries")
            logger.info(f"Found {len(permits_by_reg_no)} unique facilities")
            
            # Flatten to list based on latest_only preference
            all_versions = []
            for reg_no, versions in sorted(permits_by_reg_no.items()):
                if self.latest_only:
                    all_versions.append(versions[0])
                else:
                    all_versions.extend(versions)
            
            return all_versions
            
        except Exception as e:
            logger.error(f"Error fetching permits: {e}")
            if self.driver:
                self.driver.quit()
            return []
    
    def _extract_permits_from_table(self) -> List[Dict[str, Any]]:
        """Extract permit data from the web table."""
        table = self.driver.find_element(By.TAG_NAME, "table")
        rows = table.find_elements(By.CSS_SELECTOR, "tbody tr")
        
        permits_data = []
        
        for row in rows:
            try:
                cells = row.find_elements(By.TAG_NAME, "td")
                if len(cells) < 6:
                    continue
                
                facility_name = cells[0].text.strip()
                reg_no_cell = cells[1]
                issue_date = cells[2].text.strip()
                permit_type = cells[3].text.strip()
                county = cells[4].text.strip()
                region = cells[5].text.strip()
                
                # Get link
                links = reg_no_cell.find_elements(
                    By.CSS_SELECTOR, "a[href*='showpublisheddocument']"
                )
                if not links:
                    continue
                
                link = links[0]
                href = link.get_attribute('href')
                reg_no = link.text.strip()
                
                # Skip invalid entries
                if len(reg_no) > 15 or not reg_no or not reg_no.isdigit():
                    continue
                
                # Parse date
                try:
                    date_obj = datetime.strptime(issue_date, "%m/%d/%Y")
                    date_sortable = date_obj.strftime("%Y-%m-%d")
                    date_compact = date_obj.strftime("%Y%m%d")
                except Exception:
                    date_sortable = issue_date
                    date_compact = issue_date.replace("/", "")
                
                permits_data.append({
                    'id': reg_no,
                    'facility_name': facility_name,
                    'registration_no': reg_no,
                    'issue_date': issue_date,
                    'date_sortable': date_sortable,
                    'date_compact': date_compact,
                    'permit_type': permit_type,
                    'county': county,
                    'region': region,
                    'url': href,
                    'document_id': href.split('showpublisheddocument/')[-1].split('/')[0]
                })
                
            except Exception as e:
                logger.debug(f"Error processing row: {e}")
                continue
        
        return permits_data
    
    def download_permit(self, permit_info: Dict[str, Any], output_path: Path) -> bool:
        """
        Download a single permit PDF.
        
        Args:
            permit_info: Permit metadata dictionary
            output_path: Path to save the downloaded file
            
        Returns:
            True if successful, False otherwise
        """
        reg_no = permit_info['registration_no']
        url = permit_info['url']
        wait_time = 60
        
        marker = " [LATEST]" if permit_info.get('is_latest') else " [HISTORICAL]"
        logger.info(f"  Downloading ({permit_info['issue_date']}){marker}...")
        
        try:
            self.driver.get(url)
            time.sleep(2)
            
            # Wait for file to appear
            start_time = time.time()
            found_file = None
            
            while time.time() - start_time < wait_time:
                # Check for expected filename
                if output_path.exists() and output_path.stat().st_size > 1000:
                    found_file = output_path
                    break
                
                # Check for any file with matching registration number
                matching_files = list(self.output_dir.glob(f"{reg_no}*.pdf"))
                if matching_files:
                    newest_file = max(matching_files, key=lambda f: f.stat().st_mtime)
                    if time.time() - newest_file.stat().st_mtime < 5:
                        if newest_file != output_path:
                            newest_file.rename(output_path)
                        found_file = output_path
                        break
                
                time.sleep(0.5)
            
            if found_file:
                size_kb = found_file.stat().st_size / 1024
                logger.info(f"  ✓ Downloaded (%.0f KB){marker}", size_kb)
                return True
            else:
                logger.error(f"  ✗ File not found after {wait_time} seconds")
                return False
                
        except Exception as e:
            logger.error(f"  ✗ Error downloading: {e}")
            return False
        finally:
            time.sleep(2)  # Delay between downloads
    
    def _get_output_path(self, permit_info: Dict[str, Any]) -> Path:
        """Generate output file path for a permit."""
        return self.output_dir / permit_info['filename']
    
    def run(self):
        """Execute the scraping process."""
        try:
            super().run()
        finally:
            if self.driver:
                self.driver.quit()
                logger.info("Browser closed")
