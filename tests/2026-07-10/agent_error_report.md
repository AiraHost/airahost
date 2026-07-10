# Daily Agent Error Report

## Analysis successful

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1596737613274892756"}
AiraHost Report URL: https://www.airahost.com/r/gkyxrj55

Collected 11 checks.

### Checking Comp: https://www.airbnb.com/rooms/705327151716911393?adults=5&check_in=2026-07-22&check_out=2026-07-23&guests=5 [From Entry: Luxury 2BR Apt near Tech Companies and Stanford]
**Misalignments Found:**
- Title mismatch on date 22: Expected "Luxury 2BR Apt near Tech Companies and Stanford", got page title "500 Internal Server Error - Airbnb"
- Price mismatch on date 22: Expected "620" (Total: ) but not found in page text. (Found other prices on page: none)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/gkyxrj55
2. Click on the calendar tile for date 22
3. Click 'View' on the comparable listing card for "Luxury 2BR Apt near Tech Companies and Stanford"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-07-10/screenshot-Luxury_2BR_Apt_-22.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-07-10/page-Luxury_2BR_Apt_-22.html

### Checking Comp: https://www.airbnb.com/rooms/705327151716911393?adults=5&check_in=2026-07-23&check_out=2026-07-24&guests=5 [From Entry: Luxury 2BR Apt near Tech Companies and Stanford]
**Misalignments Found:**
- Title mismatch on date 23: Expected "Luxury 2BR Apt near Tech Companies and Stanford", got page title "500 Internal Server Error - Airbnb"
- Price mismatch on date 23: Expected "620" (Total: ) but not found in page text. (Found other prices on page: none)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/gkyxrj55
2. Click on the calendar tile for date 23
3. Click 'View' on the comparable listing card for "Luxury 2BR Apt near Tech Companies and Stanford"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-07-10/screenshot-Luxury_2BR_Apt_-23.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-07-10/page-Luxury_2BR_Apt_-23.html

## Error in analysis

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1669685800392899021"}
AiraHost Report URL: https://www.airahost.com/r/acahdur9
Error: PDP individual listing extraction failed (payload + rendered DOM): https://www.airbnb.com/rooms/1669685800392899021

## Error in analysis

Input: {"mode":"criteria","city":"Seattle","state":"Washington","beds":1,"bedrooms":1,"guests":1,"days":5}
AiraHost Report URL: https://www.airahost.com/r/hp7udzj7
Error: Service is busy. An error occurred during analysis — please try again later.

