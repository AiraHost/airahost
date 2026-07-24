# Daily Agent Error Report

## Analysis successful

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1596737613274892756"}
AiraHost Report URL: https://www.airahost.com/r/hbffp96s

Collected 20 checks.

### Checking Comp: https://www.airbnb.com/rooms/54241388?adults=5&check_in=2026-08-01&check_out=2026-08-03&guests=5 [From Entry: Large Bright Modern Central | No guest service fee]
**Misalignments Found:**
- Price mismatch on date 1: Expected "587" (Total: 900) but not found in page text. (Found other prices on page: $1,080, $986, $797, $926, $1,416, $851, $1,471, $1,292, $2,252, $826)

**Way to reproduce:**
1. Go to AiraHost Report URL: https://www.airahost.com/r/hbffp96s
2. Click on the calendar tile for date 1
3. Click 'View' on the comparable listing card for "Large Bright Modern Central | No guest service fee"

Screenshot saved to /home/runner/work/airahost/airahost/tests/2026-07-24/screenshot-Large_Bright_Mo-1.png
HTML saved to /home/runner/work/airahost/airahost/tests/2026-07-24/page-Large_Bright_Mo-1.html

## Error in analysis

Input: {"mode":"url","url":"https://www.airbnb.com/rooms/1669685800392899021"}
AiraHost Report URL: https://www.airahost.com/r/kzb28jap
Error: PDP individual listing extraction failed (payload + rendered DOM): https://www.airbnb.com/rooms/1669685800392899021

## Error in analysis

Input: {"mode":"criteria","city":"Seattle","state":"Washington","beds":1,"bedrooms":1,"guests":1,"days":5}
AiraHost Report URL: https://www.airahost.com/r/4shqcdab
Error: Service is busy. An error occurred during analysis — please try again later.

