# Daily Agent Error Report

## Analysis successful

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1596737613274892756"}
AiraHost Report URL: https://www.airahost.com/r/nfabhz3y

Collected 18 checks.

### Checking Comp: https://www.airbnb.com/rooms/705327151716911393?adults=5&check_in=2026-08-15&check_out=2026-08-16&guests=5 [From Entry: Luxury 2BR Apt near Tech Companies and Stanford]
**Misalignments Found:**
- Price mismatch on date 15: Expected "552" (Total: ) but not found in page text. (Found other prices on page: $250, $100, $10)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/nfabhz3y
2. Click on the calendar tile for date 15
3. Click 'View' on the comparable listing card for "Luxury 2BR Apt near Tech Companies and Stanford"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-08-08/screenshot-Luxury_2BR_Apt_-15.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-08-08/page-Luxury_2BR_Apt_-15.html

### Checking Comp: https://www.airbnb.com/rooms/546651466536982110?adults=5&check_in=2026-08-16&check_out=2026-08-18&guests=5 [From Entry: Silicon Valley Retreat 2BR Apt in Menlo Park]
**Misalignments Found:**
- Price mismatch on date 16: Expected "455.5" (Total: 911) but not found in page text. (Found other prices on page: $250, $100, $10)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/nfabhz3y
2. Click on the calendar tile for date 16
3. Click 'View' on the comparable listing card for "Silicon Valley Retreat 2BR Apt in Menlo Park"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-08-08/screenshot-Silicon_Valley_-16.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-08-08/page-Silicon_Valley_-16.html

### Checking Comp: https://www.airbnb.com/rooms/705327151716911393?adults=5&check_in=2026-08-16&check_out=2026-08-17&guests=5 [From Entry: Luxury 2BR Apt near Tech Companies and Stanford]
**Misalignments Found:**
- Price mismatch on date 16: Expected "620" (Total: ) but not found in page text. (Found other prices on page: $250, $100, $10)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/nfabhz3y
2. Click on the calendar tile for date 16
3. Click 'View' on the comparable listing card for "Luxury 2BR Apt near Tech Companies and Stanford"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-08-08/screenshot-Luxury_2BR_Apt_-16.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-08-08/page-Luxury_2BR_Apt_-16.html

### Checking Comp: https://www.airbnb.com/rooms/546651466536982110?adults=5&check_in=2026-08-17&check_out=2026-08-19&guests=5 [From Entry: Silicon Valley Retreat 2BR Apt in Menlo Park]
**Misalignments Found:**
- Price mismatch on date 17: Expected "455.5" (Total: 911) but not found in page text. (Found other prices on page: $250, $100, $10)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/nfabhz3y
2. Click on the calendar tile for date 17
3. Click 'View' on the comparable listing card for "Silicon Valley Retreat 2BR Apt in Menlo Park"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-08-08/screenshot-Silicon_Valley_-17.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-08-08/page-Silicon_Valley_-17.html

## Error in analysis

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1669685800392899021"}
AiraHost Report URL: https://www.airahost.com/r/5ngh7py6
Error: PDP individual listing extraction failed (payload + rendered DOM): https://www.airbnb.com/rooms/1669685800392899021

## Error in analysis

Input: {"mode":"criteria","city":"Seattle","state":"Washington","beds":1,"bedrooms":1,"guests":1,"days":5}
AiraHost Report URL: https://www.airahost.com/r/u2i5akrp
Error: Service is busy. An error occurred during analysis — please try again later.

