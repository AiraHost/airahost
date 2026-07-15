# Daily Agent Error Report

## Analysis successful

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1596737613274892756"}
AiraHost Report URL: https://www.airahost.com/r/t8gvdmpm

Collected 21 checks.

### Checking Comp: https://www.airbnb.com/rooms/1149339554648303407?adults=5&check_in=2026-07-22&check_out=2026-07-23&guests=5 [From Entry: 5mins walk 2 PA DT, Stanford, Dinning&Markets 2B2B]
**Misalignments Found:**
- Price mismatch on date 22: Expected "565" (Total: ) but not found in page text. (Found other prices on page: none)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/t8gvdmpm
2. Click on the calendar tile for date 22
3. Click 'View' on the comparable listing card for "5mins walk 2 PA DT, Stanford, Dinning&Markets 2B2B"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-07-15/screenshot-5mins_walk_2_PA-22.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-07-15/page-5mins_walk_2_PA-22.html

## Error in analysis

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1669685800392899021"}
AiraHost Report URL: https://www.airahost.com/r/5k5ngneb
Error: PDP individual listing extraction failed (payload + rendered DOM): https://www.airbnb.com/rooms/1669685800392899021

## Error in analysis

Input: {"mode":"criteria","city":"Seattle","state":"Washington","beds":1,"bedrooms":1,"guests":1,"days":5}
AiraHost Report URL: https://www.airahost.com/r/9cezejry
Error: Service is busy. An error occurred during analysis — please try again later.

