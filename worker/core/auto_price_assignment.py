
import datetime
import json
import logging
import urllib.request
import urllib.error
from playwright.sync_api import sync_playwright

# No logging.basicConfig() here. main.py imports this module before installing
# its own root handlers, so basicConfig's stderr handler was added first and
# every worker log record was then emitted twice — once unformatted by that
# handler and once by main.py's formatted console handler. That is the
# duplicated console/formatted output seen throughout production logs.
# A library module must leave root-logger configuration to the entrypoint.
logger = logging.getLogger(__name__)

def load_cookies_from_file(filepath: str) -> dict:
    """Helper to load cookies from a JSON file (e.g., exported from browser)."""
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            data = json.load(f)
            # If it's a list of cookie dicts (e.g., from Playwright or EditThisCookie)
            if isinstance(data, list):
                return {cookie['name']: cookie['value'] for cookie in data if 'name' in cookie and 'value' in cookie}
            # If it's already a dictionary of key-value pairs
            if isinstance(data, dict):
                return data
    except Exception as e:
        logger.error(f"Failed to load cookies from {filepath}: {e}")
    return {}

def load_cookies_from_cdp(cdp_url: str, domain: str | None = None) -> dict:
    """Grabs Airbnb cookies from a running Playwright CDP browser session."""
    with sync_playwright() as p:
        try:
            browser = p.chromium.connect_over_cdp(cdp_url)
            contexts = browser.contexts if browser.contexts else [browser.new_context()]

            cookies = []
            for context in contexts:
                cookies.extend(context.cookies())

            result = {}
            domain_checks = []
            if domain:
                domain_checks.append(domain)
            domain_checks.extend(["airbnb.ca", "airbnb.com"])

            for cookie in cookies:
                cookie_domain = cookie.get("domain", "")
                if any(check in cookie_domain for check in domain_checks):
                    result[cookie["name"]] = cookie["value"]

            return result
        except Exception as e:
            logger.error(f"Failed to load cookies from CDP {cdp_url}: {e}")
            return {}

def assign_price_request(listing_id: str, date: str, price: int, *, locale="en-CA", currency="CAD", cookies: dict | None = None) -> dict:
    """
    Sets the nightly price directly using Airbnb's GraphQL API.
    """
    if not cookies:
        return {"ok": False, "error": "auth required"}

    if not listing_id:
        return {"ok": False, "error": "listing_id must be provided."}
    
    try:
        datetime.datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return {"ok": False, "error": "Invalid date format. Use YYYY-MM-DD."}
        
    if price < 1:
        return {"ok": False, "error": "Price must be a positive number."}

    # Prepare URL
    sha256_hash = "1de2c4649768cacd7bf82368dc9d298e7e7472b57ec8086c087b7524be2f03ef"
    url = f"https://www.airbnb.ca/api/v3/EditPanelPricingSettingsMutation/{sha256_hash}?operationName=EditPanelPricingSettingsMutation&locale={locale}&currency={currency}"

    # Prepare Headers
    cookie_str = "; ".join([f"{k}={v}" for k, v in cookies.items()])
    headers = {
        "content-type": "application/json",
        "x-airbnb-api-key": "d306zoyjsyarp7ifhu67rjxn52tv0t20",
        "x-airbnb-graphql-platform": "web",
        "x-airbnb-graphql-platform-client": "minimalist-niobe",
        "x-airbnb-supports-airlock-v2": "true",
        "x-csrf-without-token": "1",
        "accept": "*/*",
        "cookie": cookie_str
    }

    # Prepare Payload
    payload = {
        "operationName": "EditPanelPricingSettingsMutation",
        "variables": {
            "input": {
                "listingId": listing_id,
                "selectedDateRanges": [
                    { "startDate": date, "endDate": date }
                ],
                "nightlyPriceAmount": price,
                "turnOnSmartPricing": False,
                "isCalendarV2": True
            }
        },
        "extensions": {
            "persistedQuery": {
                "version": 1,
                "sha256Hash": sha256_hash
            }
        }
    }

    req = urllib.request.Request(url, data=json.dumps(payload).encode('utf-8'), headers=headers, method="POST")

    try:
        with urllib.request.urlopen(req) as response:
            raw_body = response.read().decode('utf-8', errors='replace')
            logger.info(
                "[price_assign] listing=%s date=%s price=%s status=%s",
                listing_id,
                date,
                price,
                response.status,
            )
            if response.status != 200:
                return {"ok": False, "error": f"HTTP {response.status}: {response.reason}"}

            resp_data = json.loads(raw_body) if raw_body else {}
            
            if "errors" in resp_data:
                return {"ok": False, "error": resp_data["errors"]}
                
            return {"ok": True}
            
    except urllib.error.HTTPError as e:
        raw_err = e.read().decode('utf-8', errors='replace')
        logger.warning(
            "[price_assign] listing=%s date=%s price=%s status=%s",
            listing_id,
            date,
            price,
            getattr(e, "code", "HTTPError"),
        )
        try:
            error_resp = json.loads(raw_err) if raw_err else {}
            if "errors" in error_resp:
                return {"ok": False, "error": error_resp["errors"]}
            return {"ok": False, "error": f"HTTPError {e.code}: {error_resp}"}
        except json.JSONDecodeError:
            return {"ok": False, "error": f"HTTPError {e.code}: {raw_err or e.reason}"}
    except Exception as e:
        return {"ok": False, "error": f"Request failed: {str(e)}"}

def assign_prices_calendar_request(listing_id: str, calendar_dict: dict, *, locale="en-CA", currency="CAD", cookies: dict | None = None) -> dict:
    """
    Batch assigns prices for a given listing and a dictionary of dates.
    """
    summary = {"ok": True, "results": {}, "errors": [], "skipped": []}
    if not cookies:
        return {"ok": False, "error": "auth required"}
    for date in sorted(calendar_dict.keys()):
        price = calendar_dict[date]
        logger.info(f"Setting price for {date} to ${price} via API")
        result = assign_price_request(listing_id=listing_id, date=date, price=price, locale=locale, currency=currency, cookies=cookies)
        summary["results"][date] = result
        if not result.get("ok"):
            summary["ok"] = False
            summary["errors"].append({date: result.get("error")})
    return summary

def assign_price(listing_id: str, date: str, price: int, *, cdp_url: str, cookies: dict | None = None) -> dict:
    """
    Sets the price for a given listing and date on Airbnb using a direct API request.

    Args:
        listing_id: The ID of the Airbnb listing.
        date: The date to set the price for (YYYY-MM-DD).
        price: The price to set.
        cdp_url: The CDP URL of the running browser instance.
        cookies: Optional dictionary of cookies. If not provided, will be fetched via CDP.

    Returns:
        A dictionary with "ok": True on success, or "ok": False and an "error" message on failure.
    """

    # Input validation
    if not listing_id:
        return {"ok": False, "error": "listing_id must be provided."}
    try:
        datetime.datetime.strptime(date, '%Y-%m-%d')
    except ValueError:
        return {"ok": False, "error": "Invalid date format. Use YYYY-MM-DD."}
    if price < 1:
        return {"ok": False, "error": "Price must be a positive number."}

    # Always recapture fresh cookies for each assignment call.
    if cookies is not None:
        logger.info("Caller-provided cookies ignored; recapturing fresh cookies.")
    logger.info("Recapturing fresh cookies via CDP browser context.")
    cookies = load_cookies_from_cdp(cdp_url)
    if not cookies:
        return {"ok": False, "error": "Could not recapture cookies from CDP session."}

    return assign_price_request(listing_id=listing_id, date=date, price=price, cookies=cookies)

def assign_prices_calendar(listing_id: str, calendar: dict[str, int], *, cdp_url: str, cookies: dict | None = None) -> dict:
    """
    Batch assigns prices for a given listing and a dictionary of dates.
    """
    # Always recapture fresh cookies for each batch assignment call.
    if cookies is not None:
        logger.info("Caller-provided cookies ignored; recapturing fresh cookies.")
    logger.info("Recapturing fresh cookies via CDP browser context.")
    cookies = load_cookies_from_cdp(cdp_url)
    if not cookies:
        return {"ok": False, "error": "Could not recapture cookies from CDP session."}

    return assign_prices_calendar_request(listing_id=listing_id, calendar_dict=calendar, cookies=cookies)
