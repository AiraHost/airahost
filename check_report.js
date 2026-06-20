/* eslint-disable @typescript-eslint/no-require-imports */
const { chromium } = require('playwright');

(async () => {
  const browser = await chromium.launch({ headless: true });
  const page = await browser.newPage();
  
  await page.goto('https://www.airahost.com/r/azs2csep', { waitUntil: 'networkidle' });
  await page.content();
  
  // Find all links
  const links = await page.evaluate(() => {
    return Array.from(document.querySelectorAll('a')).map(a => a.href);
  });
  
  console.log("Found links:", links.filter(l => l.includes('airbnb.com')));
  
  await browser.close();
})();
