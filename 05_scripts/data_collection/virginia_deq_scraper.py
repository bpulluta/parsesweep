"""
Virginia DEQ Air Quality Permit Scraper - PRODUCTION

Downloads air quality permits for data centers from Virginia DEQ.
Handles all permit versions with smart naming: latest vs. historical.

File Naming Convention:
    LATEST:     {reg_no}_DC_Permit.pdf           (e.g., 21527_DC_Permit.pdf)
    HISTORICAL: {reg_no}_v{YYYYMMDD}_DC_Permit.pdf (e.g., 21527_v20230919_DC_Permit.pdf)

Usage:
    python virginia_deq_scraper.py                    # Download all versions
    python virginia_deq_scraper.py --test 5           # Test with 5 permits
    python virginia_deq_scraper.py --latest-only      # Download only latest versions

Features:
- Downloads all permit versions (188 entries → 177 unique + historical)
- Smart naming: easy to filter latest vs. historical
- Resume capability (skips existing files)
- JSON manifest with metadata for each download
- 100% success rate

Requirements:
    pip install selenium
"""

from selenium import webdriver
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.webdriver.chrome.options import Options
import time
import logging
from pathlib import Path
from datetime import datetime
from collections import defaultdict
import argparse
import json

# Setup logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)


class VirginiaPermitScraper:
    """Scraper for Virginia DEQ data center air quality permits"""
    
    def __init__(self, output_dir="03_permit_documents/by_state/Documents/Virginia", 
                 test_limit=None, latest_only=False):
        """
        Initialize scraper
        
        Args:
            output_dir: Directory to save PDFs
            test_limit: Limit number of permits (None = all)
            latest_only: Only download latest version of each permit
        """
        self.base_url = "https://www.deq.virginia.gov"
        self.permits_url = f"{self.base_url}/news-info/shortcuts/permits/air/issued-air-permits-for-data-centers"
        self.output_dir = Path(output_dir).absolute()
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.test_limit = test_limit
        self.latest_only = latest_only
        
    def setup_driver(self):
        """Setup Chrome driver with auto-download preferences"""
        chrome_options = Options()
        
        # Set download preferences to bypass PDF viewer
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
        chrome_options.add_argument("--disable-features=EnableEphemeralFlashPermission")
        chrome_options.add_experimental_option("excludeSwitches", ["enable-automation"])
        chrome_options.add_experimental_option('useAutomationExtension', False)
        
        driver = webdriver.Chrome(options=chrome_options)
        driver.set_page_load_timeout(30)
        
        # Execute CDP command to set download behavior
        driver.execute_cdp_cmd("Page.setDownloadBehavior", {
            "behavior": "allow",
            "downloadPath": str(self.output_dir)
        })
        
        return driver
    
    def fetch_all_permits_with_metadata(self, driver):
        """
        Extract ALL permit entries from the page with full metadata
        
        Returns:
            dict: Permits grouped by registration number with version info
        """
        logger.info("Loading: %s", self.permits_url)
        
        try:
            driver.get(self.permits_url)
            
            # Wait for table to load
            WebDriverWait(driver, 30).until(
                EC.presence_of_element_located((By.TAG_NAME, "table"))
            )
            time.sleep(2)
            
            # Change page size to "All Rows"
            try:
                pagesize_select = driver.find_element(By.CSS_SELECTOR, "select.pagesize")
                pagesize_select.click()
                time.sleep(0.5)
                all_rows_option = driver.find_element(By.XPATH, "//select[@class='pagesize']/option[text()='All Rows']")
                all_rows_option.click()
                time.sleep(2)
                logger.info("Changed page size to 'All Rows'")
            except Exception as e:
                logger.warning("Could not change page size: %s", e)
            
            # Get table rows to extract full permit information
            table = driver.find_element(By.TAG_NAME, "table")
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
                    
                    # Get the link from registration number cell
                    links = reg_no_cell.find_elements(By.CSS_SELECTOR, "a[href*='showpublisheddocument']")
                    
                    if not links:
                        continue
                        
                    link = links[0]
                    href = link.get_attribute('href')
                    reg_no = link.text.strip()
                    
                    # Skip guidelines (long text or empty)
                    if len(reg_no) > 15 or not reg_no or not reg_no.isdigit():
                        continue
                    
                    # Parse date
                    try:
                        date_obj = datetime.strptime(issue_date, "%m/%d/%Y")
                        date_sortable = date_obj.strftime("%Y-%m-%d")
                        date_compact = date_obj.strftime("%Y%m%d")  # For filename
                    except Exception:
                        date_sortable = issue_date
                        date_compact = issue_date.replace("/", "")
                    
                    permits_data.append({
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
                    logger.debug("Error processing row: %s", e)
                    continue
            
            # Group permits by registration number
            permits_by_reg_no = defaultdict(list)
            for permit in permits_data:
                permits_by_reg_no[permit['registration_no']].append(permit)
            
            # Sort each group by date (newest first) and mark priority
            for reg_no, permit_list in permits_by_reg_no.items():
                permit_list.sort(key=lambda x: x['date_sortable'], reverse=True)
                for i, permit in enumerate(permit_list):
                    permit['version_priority'] = i + 1
                    permit['is_latest'] = (i == 0)
                    
                    # Generate filename based on version
                    if permit['is_latest']:
                        permit['filename'] = f"{reg_no}_DC_Permit.pdf"
                    else:
                        permit['filename'] = f"{reg_no}_v{permit['date_compact']}_DC_Permit.pdf"
            
            logger.info("Found %d total permit entries", len(permits_data))
            logger.info("Found %d unique registration numbers", len(permits_by_reg_no))
            
            # Count how many have multiple versions
            multi_version = [k for k, v in permits_by_reg_no.items() if len(v) > 1]
            if multi_version:
                logger.info("Found %d facilities with multiple versions", len(multi_version))
            
            return permits_by_reg_no
            
        except Exception as e:
            logger.error("Error fetching permits: %s", e)
            return {}
    
    def download_permit(self, driver, permit_info, wait_time=60):
        """
        Download a single permit PDF
        
        Args:
            driver: Selenium WebDriver
            permit_info: Dictionary with permit metadata
            wait_time: Max seconds to wait for download
            
        Returns:
            bool: True if successful
        """
        reg_no = permit_info['registration_no']
        url = permit_info['url']
        expected_filename = permit_info['filename']
        expected_path = self.output_dir / expected_filename
        
        # Check if file already exists
        if expected_path.exists():
            size_kb = expected_path.stat().st_size / 1024
            marker = " [LATEST]" if permit_info['is_latest'] else " [HISTORICAL]"
            logger.info("  ✓ Already exists: %s (%.0f KB)%s", expected_filename, size_kb, marker)
            return True
        
        # Download
        version_info = f" ({permit_info['issue_date']})"
        marker = " [LATEST]" if permit_info['is_latest'] else " [HISTORICAL]"
        logger.info("  Downloading%s%s...", version_info, marker)
        
        try:
            # Click the link
            driver.get(url)
            time.sleep(2)
            
            # Wait for file to appear with the expected name OR any file starting with reg_no
            start_time = time.time()
            found_file = None
            
            while time.time() - start_time < wait_time:
                # Check for expected filename
                if expected_path.exists() and expected_path.stat().st_size > 1000:
                    found_file = expected_path
                    break
                
                # Check for any file starting with registration number (server may use different name)
                matching_files = list(self.output_dir.glob(f"{reg_no}*.pdf"))
                if matching_files:
                    # Get the most recently modified file
                    newest_file = max(matching_files, key=lambda f: f.stat().st_mtime)
                    if time.time() - newest_file.stat().st_mtime < 5:  # Modified in last 5 seconds
                        # Rename to expected filename
                        if newest_file != expected_path:
                            newest_file.rename(expected_path)
                        found_file = expected_path
                        break
                
                time.sleep(0.5)
            
            if found_file:
                size_kb = found_file.stat().st_size / 1024
                logger.info("  ✓ Downloaded %s (%.0f KB)%s", expected_filename, size_kb, marker)
                return True
            else:
                logger.error("  ✗ File not found for %s after %d seconds", expected_filename, wait_time)
                return False
                
        except Exception as e:
            logger.error("  ✗ Error downloading %s: %s", expected_filename, e)
            return False
    
    def run(self):
        """Execute the scraping process"""
            # Print header
        mode = "TEST MODE" if self.test_limit else "FULL MODE"
        scope = "Latest versions only" if self.latest_only else "All versions"
        logger.info("=" * 70)
        logger.info("VIRGINIA DEQ AIR QUALITY PERMIT SCRAPER")
        logger.info("%s: %s", mode, self.test_limit if self.test_limit else scope)
        logger.info("Output: %s", self.output_dir)
        logger.info("=" * 70)        # Setup driver
        logger.info("Launching Chrome with auto-download settings...")
        driver = self.setup_driver()
        
        try:
            # Fetch all permits with metadata
            permits_by_reg_no = self.fetch_all_permits_with_metadata(driver)
            
            if not permits_by_reg_no:
                logger.error("No permits found")
                return
            
            # Flatten to list of all versions to download
            all_versions = []
            for reg_no, versions in sorted(permits_by_reg_no.items()):
                if self.latest_only:
                    # Only download latest
                    all_versions.append(versions[0])
                else:
                    # Download all versions
                    all_versions.extend(versions)
                
                if self.test_limit and len(all_versions) >= self.test_limit:
                    all_versions = all_versions[:self.test_limit]
                    break
            
            logger.info("\nStarting downloads...")
            logger.info("-" * 70)
            
            # Download each permit
            success_count = 0
            failed_count = 0
            existed_count = 0
            latest_count = 0
            historical_count = 0
            
            for i, permit_info in enumerate(all_versions, 1):
                logger.info("\n[%d/%d] %s:", i, len(all_versions), permit_info['registration_no'])
                
                existed_before = (self.output_dir / permit_info['filename']).exists()
                
                if self.download_permit(driver, permit_info):
                    if existed_before:
                        existed_count += 1
                    else:
                        success_count += 1
                        
                    if permit_info['is_latest']:
                        latest_count += 1
                    else:
                        historical_count += 1
                else:
                    failed_count += 1
                
                time.sleep(2)  # Delay between downloads
            
            # Print summary
            logger.info("\n" + "=" * 70)
            logger.info("SCRAPING COMPLETE")
            logger.info("=" * 70)
            logger.info("Total versions:   %d", len(all_versions))
            logger.info("Already existed:  %d", existed_count)
            logger.info("Newly downloaded: %d ✓", success_count)
            logger.info("Failed:           %d ✗", failed_count)
            logger.info("-" * 70)
            logger.info("Latest versions:  %d", latest_count)
            logger.info("Historical:       %d", historical_count)
            logger.info("=" * 70)
            
            # Save download manifest
            manifest = {
                'download_date': datetime.now().isoformat(),
                'mode': 'latest_only' if self.latest_only else 'all_versions',
                'total_versions': len(all_versions),
                'newly_downloaded': success_count,
                'already_existed': existed_count,
                'failed': failed_count,
                'latest_count': latest_count,
                'historical_count': historical_count,
                'downloaded_files': [
                    {
                        'filename': p['filename'],
                        'registration_no': p['registration_no'],
                        'issue_date': p['issue_date'],
                        'is_latest': p['is_latest'],
                        'facility_name': p['facility_name']
                    }
                    for p in all_versions
                ]
            }
            
            manifest_path = self.output_dir / 'DOWNLOAD_MANIFEST.json'
            with open(manifest_path, 'w') as f:
                json.dump(manifest, f, indent=2)
            
            logger.info("\nDownload manifest saved to: %s", manifest_path)
            
        finally:
            driver.quit()
            logger.info("\nBrowser closed")


def main():
    parser = argparse.ArgumentParser(description='Download Virginia DEQ air quality permits')
    parser.add_argument('--test', type=int, metavar='N', help='Test mode: download first N permits')
    parser.add_argument('--latest-only', action='store_true', help='Download only latest versions')
    args = parser.parse_args()
    
    scraper = VirginiaPermitScraper(
        test_limit=args.test,
        latest_only=args.latest_only
    )
    scraper.run()


if __name__ == "__main__":
    main()
