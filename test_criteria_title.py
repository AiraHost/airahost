import json
import logging
import sys
from worker.scraper.parsers import parse_search_listing_context

def main():
    logging.basicConfig(level=logging.DEBUG, stream=sys.stdout)
    logger = logging.getLogger("test_title")
    
    # Mock Airbnb search response for criteria search
    mock_payload = {
        "data": {
            "presentation": {
                "staysSearch": {
                    "results": {
                        "searchResults": [
                            {
                                "listingId": "123",
                                "listing": {
                                    "id": "123",
                                    "name": "Cozy Austin Home",
                                    "title": "Beautiful house in Austin",
                                    "subtitle": "Entire home in Austin"
                                },
                                "pricingQuote": {
                                    "structuredStayDisplayPrice": {
                                        "primaryLine": {
                                            "price": "$100 USD"
                                        }
                                    }
                                }
                            },
                            {
                                "demandStayListing": {
                                    "id": "bGVnYWN5X2lkXzQ1Ng==", # 456
                                    "name": "Another Austin Home",
                                    "title": "Luxury Austin apartment"
                                }
                            }
                        ]
                    }
                }
            }
        }
    }
    
    context = parse_search_listing_context(mock_payload)
    print("Parsed Context:")
    for cid, row in context.items():
        print(f"ID: {cid}, Title: {row.get('title')}")
        
    # Check if 'title' is correctly extracted
    if context.get("123", {}).get("title") == "Beautiful house in Austin":
        print("SUCCESS: Listing 123 title extracted correctly.")
    else:
        print("FAIL: Listing 123 title mismatch.")
        
if __name__ == "__main__":
    main()
