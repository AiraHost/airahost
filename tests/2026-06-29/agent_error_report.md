# Daily Agent Error Report

## Analysis successful

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1596737613274892756"}
AiraHost Report URL: https://www.airahost.com/r/6n6esxd4

Collected 0 checks.

## Analysis successful

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1669685800392899021"}
AiraHost Report URL: https://www.airahost.com/r/zrrj4db8

Collected 21 checks.

### Checking Comp: https://www.airbnb.com/rooms/838726216712270836?adults=4&check_in=2026-07-06&check_out=2026-07-07&guests=4 [From Entry: Chic 2BR Townhome Near Downtown Seattle & Alki]
**Misalignments Found:**
- Price mismatch on date 6: Expected "333" (Total: ) but not found in page text. (Found other prices on page: $292)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/zrrj4db8
2. Click on the calendar tile for date 6
3. Click 'View' on the comparable listing card for "Chic 2BR Townhome Near Downtown Seattle & Alki"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-06-29/screenshot-Chic_2BR_Townho-6.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-06-29/page-Chic_2BR_Townho-6.html

### Checking Comp: https://www.airbnb.com/rooms/28202957?adults=4&check_in=2026-07-08&check_out=2026-07-10&guests=4 [From Entry: Stunning Views & Sunset Deck | Near Climate Pledge]
**Misalignments Found:**
- Price mismatch on date 8: Expected "403" (Total: 806) but not found in page text. (Found other prices on page: none)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/zrrj4db8
2. Click on the calendar tile for date 8
3. Click 'View' on the comparable listing card for "Stunning Views & Sunset Deck | Near Climate Pledge"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-06-29/screenshot-Stunning_Views_-8.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-06-29/page-Stunning_Views_-8.html

### Checking Comp: https://www.airbnb.com/rooms/1692429278438610460?adults=4&check_in=2026-07-09&check_out=2026-07-11&guests=4 [From Entry: Warm Capitol Hill Townhome near Light Rail with AC]
**Misalignments Found:**
- Price mismatch on date 9: Expected "388" (Total: 776) but not found in page text. (Found other prices on page: none)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/zrrj4db8
2. Click on the calendar tile for date 9
3. Click 'View' on the comparable listing card for "Warm Capitol Hill Townhome near Light Rail with AC"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-06-29/screenshot-Warm_Capitol_Hi-9.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-06-29/page-Warm_Capitol_Hi-9.html

## Analysis successful

Input: {"mode":"criteria","city":"Seattle","state":"Washington","beds":1,"bedrooms":1,"guests":1,"days":5}
AiraHost Report URL: https://www.airahost.com/r/cmncigrv

Collected 15 checks.


✅ All checks passed! No pricing misalignments found.

