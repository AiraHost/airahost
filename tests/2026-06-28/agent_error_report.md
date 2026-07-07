# Daily Agent Error Report

## Error in analysis

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1596737613274892756"}
AiraHost Report URL: https://www.airahost.com/r/qwp7ufk8
Error: PDP individual listing extraction failed (payload + rendered DOM): https://www.airbnb.com/rooms/1596737613274892756

## Analysis successful

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1669685800392899021"}
AiraHost Report URL: https://www.airahost.com/r/j2wtskx2

Collected 21 checks.

### Checking Comp: https://www.airbnb.com/rooms/1694685400815666796?check_in=2026-07-05&check_out=2026-07-07&guests=4&adults=4 [From Entry: Downtown Seattle Loft | Rooftop & Skyline Views]
**Misalignments Found:**
- Price mismatch on date 5: Expected "393.5" (Total: 618) but not found in page text. (Found other prices on page: $856, $787)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/j2wtskx2
2. Click on the calendar tile for date 5
3. Click 'View' on the comparable listing card for "Downtown Seattle Loft | Rooftop & Skyline Views"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-06-28/screenshot-Downtown_Seattl-5.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-06-28/page-Downtown_Seattl-5.html

## Error in analysis

Input: {"mode":"criteria","city":"Seattle","state":"Washington","beds":1,"bedrooms":1,"guests":1,"days":5}
AiraHost Report URL: https://www.airahost.com/r/unw2xnkq
Error: Service is busy. An error occurred during analysis — please try again later.

