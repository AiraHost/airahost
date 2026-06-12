const { chromium } = require('playwright');
const fs = require('fs');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  
  await page.goto('https://www.airbnb.com/rooms/776411339068715820?check_in=2026-06-23&check_out=2026-06-25', { waitUntil: 'domcontentloaded' });
  await page.waitForTimeout(3000);
  
  const text = await page.evaluate(() => document.body.innerText);
  fs.writeFileSync('page_text.txt', text);
  
  await browser.close();
})();
