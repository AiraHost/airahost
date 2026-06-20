import { test, expect, Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';

/* eslint-disable @typescript-eslint/no-explicit-any */

test.describe('Daily Agent - AiraHost Analysis Checker', () => {
  // Use a long timeout (15 mins) because AiraHost analysis might take time,
  // and we are scraping multiple Airbnb URLs.
  test.setTimeout(900000); 

  // Explicitly use production site since local test environments may not run the ML workers
  test.use({ baseURL: 'https://airahost.com' });

  test('run URL analysis and check comps', async ({ page }) => {
    const urlsToTest = [
      'https://www.airbnb.com/rooms/1596737613274892756',
      'https://www.airbnb.com/rooms/1669685800392899021',
    ];

    const dateStr = new Date().toISOString().split('T')[0];
    const reportDir = path.join(process.cwd(), 'tests', dateStr);
    if (!fs.existsSync(reportDir)) {
      fs.mkdirSync(reportDir, { recursive: true });
    }
    const reportPath = path.join(reportDir, 'agent_error_report.md');
    fs.writeFileSync(reportPath, '# Daily Agent Error Report\n\n');

    for (const airbnbUrl of urlsToTest) {
      await runAnalysisAndCheck(page, { mode: 'url', url: airbnbUrl }, reportPath);
    }
    
    // Also test criteria analysis
    await runAnalysisAndCheck(page, {
      mode: 'criteria',
      city: 'Seattle',
      state: 'Washington',
      beds: 1,
      bedrooms: 1,
      guests: 1,
      days: 5
    }, reportPath);
  });
});

async function runAnalysisAndCheck(page: Page, input: any, reportPath: string) {
  await page.goto('https://airahost.com/tool');

  if (input.mode === 'url') {
    await page.getByRole('button', { name: 'I have a listing URL' }).click();
    await page.getByPlaceholder('https://airbnb.com/rooms/').fill(input.url);
  } else {
    await page.getByRole('button', { name: 'Search by criteria' }).click();
    await page.getByPlaceholder('e.g. New York, Taipei').fill(input.city);
    await page.getByPlaceholder('e.g. CA').fill(input.state);
    await setStepperValue(page, 'Bedrooms', input.bedrooms);
    await setStepperValue(page, 'Max guests', input.guests);
  }

  await page.getByRole('button', { name: 'Continue' }).click();

  // Set dates (5 days span)
  if (input.mode === 'criteria' && input.days) {
     const startDateInput = page.locator('input[type="date"]').nth(0);
     const endDateInput = page.locator('input[type="date"]').nth(1);
     const start = new Date();
     start.setDate(start.getDate() + 7); // Start 1 week from now
     const end = new Date(start);
     end.setDate(end.getDate() + input.days);
     
     await startDateInput.fill(start.toISOString().split('T')[0]);
     await endDateInput.fill(end.toISOString().split('T')[0]);
  }
  await page.getByRole('button', { name: 'Continue' }).click();

  // Benchmark - just continue
  await page.getByRole('button', { name: 'Generate Revenue Report' }).click();

  // Poll the report endpoint directly; browser response events can be missed between waits.
  await page.waitForURL(/\/r\/[^/?#]+/, { timeout: 60000 });
  const reportApiUrl = new URL(`/api${new URL(page.url()).pathname}`, page.url()).toString();
  let reportData: any = null;

  await expect.poll(async () => {
    try {
      const pollResponse = await page.context().request.get(reportApiUrl, { timeout: 30000 });
      if (!pollResponse.ok()) {
        return `http:${pollResponse.status()}`;
      }
      reportData = await pollResponse.json();
      return reportData.status;
    } catch (e: any) {
      return `fetch_error:${e.message}`;
    }
  }, {
    intervals: [3000],
    timeout: 600000,
    message: `Report did not finish for ${JSON.stringify(input)}`,
  }).toMatch(/^(ready|error)$/);

  if (reportData.status === 'error') {
    appendReport(reportPath, `## Error in analysis\n\nInput: ${JSON.stringify(input)}\nAiraHost Report URL: ${page.url()}\nError: ${reportData.errorMessage}\n\n`);
    return;
  }

  const reportUrl = page.url();
  appendReport(reportPath, `## Analysis successful\n\nInput: ${JSON.stringify(input)}\nAiraHost Report URL: ${reportUrl}\n\n`);

  const compsToTest: { title: string, compUrl: string, expectedPrice: string, expectedTotal: string, targetDate: string }[] = [];
  
  // Interact with UI to get dates and prices
  const dateButtons = page.locator('.grid-cols-7 button:has-text("$")');
  const dateCount = await dateButtons.count();
  
  for (let i = 0; i < dateCount; i++) {
     const btn = dateButtons.nth(i);
     const dateTextRaw = await btn.innerText();
     const targetDateLabel = dateTextRaw.split('\n')[0] || `Day-${i}`;
     
     await btn.click();
     await page.waitForTimeout(1000); // wait for comps to filter
     
     const compCards = page.locator('[data-testid="comparable-card"]');
     const compCount = await compCards.count();
     
     for (let j = 0; j < Math.min(3, compCount); j++) {
        const card = compCards.nth(j);
        const viewLink = card.locator('a', { hasText: 'View' }).first();
        if (await viewLink.count() === 0) continue;
        
        const compUrl = await viewLink.getAttribute('href');
        const title = (await card.locator('.truncate').textContent()) || 'Unknown';
        const cardText = await card.innerText();
        
        let expectedTotal = '';
        let expectedPrice = '';
        const totalMatch = cardText.match(/From \$([0-9,]+(?:\.[0-9]+)?)\s*total/);
        if (totalMatch) expectedTotal = totalMatch[1].replace(/,/g, '');
        
        const nightMatch = cardText.match(/\$([0-9,]+(?:\.[0-9]+)?)\s*\n?\s*\/\s*night/);
        if (nightMatch) expectedPrice = nightMatch[1].replace(/,/g, '');
        
        if (compUrl) {
           compsToTest.push({ title, compUrl, expectedPrice, expectedTotal, targetDate: targetDateLabel });
        }
     }
     
     // un-click
     await btn.click();
     await page.waitForTimeout(500);
  }

  
  if (input.mode === 'url') {
      const userListingUrl = input.url;
      const calendar = reportData.calendar || [];
      for (const day of calendar) {
          if (day.userPrice || day.targetListingPrice || day.median_price || day.observedListingPrice) {
              const uPrice = day.userPrice || day.targetListingPrice || day.observedListingPrice;
              if (uPrice) {
                  compsToTest.push({
                      title: "USER_LISTING",
                      compUrl: userListingUrl,
                      expectedPrice: uPrice.toString(),
                      expectedTotal: "",
                      targetDate: day.date || "UnknownDate"
                  });
              }
          }
      }
  }

  appendReport(reportPath, `Collected ${compsToTest.length} checks.\n\n`);

  let anyErrorsFound = false;
  for (const comp of compsToTest) {
      
      
      try {
        let finalUrl = comp.compUrl;
        try {
           const u = new URL(finalUrl);
           u.searchParams.set('currency', 'USD');
           finalUrl = u.toString();
        } catch {
           finalUrl += (finalUrl.includes('?') ? '&' : '?') + 'currency=USD';
        }
        
        await page.goto(finalUrl, { waitUntil: 'domcontentloaded' });
        await page.waitForTimeout(4000); // let airbnb render completely
        
        const dateStr = new Date().toISOString().split('T')[0];
        const captureDir = path.join(process.cwd(), 'tests', dateStr);
        if (!fs.existsSync(captureDir)) {
          fs.mkdirSync(captureDir, { recursive: true });
        }
        
        // Use a safe filename
        const safeTitle = comp.title.replace(/[^a-z0-9]/gi, '_').substring(0, 15);
        const screenshotPath = path.join(captureDir, `screenshot-${safeTitle}-${comp.targetDate}.png`);
        const htmlPath = path.join(captureDir, `page-${safeTitle}-${comp.targetDate}.html`);
        
        await page.screenshot({ path: screenshotPath, fullPage: true });
        const htmlContent = await page.content();
        fs.writeFileSync(htmlPath, htmlContent);

        const pageTitle = await page.title();
        const pageText = await page.evaluate(() => document.body.innerText);
        const errors = [];
        
        // Name aligns
        if (comp.title && comp.title !== 'Unknown' && !pageTitle.toLowerCase().includes(comp.title.toLowerCase().substring(0, 10))) {
          errors.push(`- Title mismatch on date ${comp.targetDate}: Expected "${comp.title}", got page title "${pageTitle}"`);
        }
        
        // Price scraped aligns
        if (comp.expectedPrice || comp.expectedTotal) {
           let found = false;
           const pagePrices = (pageText.match(/\$([0-9,]+(?:\.[0-9]+)?)/g) || []).map(p => parseFloat(p.replace(/[$,]/g, '')));
           if (comp.expectedPrice) {
               const expected = parseFloat(comp.expectedPrice);
               if (!isNaN(expected)) {
                   found = pagePrices.some(p => Math.abs(p - expected) / expected <= 0.02);
               }
           }
           if (!found && comp.expectedTotal) {
               const expected = parseFloat(comp.expectedTotal);
               if (!isNaN(expected)) {
                   found = pagePrices.some(p => Math.abs(p - expected) / expected <= 0.02);
               }
           }
           
           if (!found) {
              const otherPricesMatch = pageText.match(/\$([0-9,]+(?:\.[0-9]+)?)/g);
              const otherPrices = otherPricesMatch ? Array.from(new Set(otherPricesMatch)).join(', ') : 'none';
              errors.push(`- Price mismatch on date ${comp.targetDate}: Expected "${comp.expectedPrice}" (Total: ${comp.expectedTotal}) but not found in page text. (Found other prices on page: ${otherPrices})`);
           }
        }
        
        if (errors.length > 0) {
          const reproduceSteps = `
**Way to reproduce:**
1. Go to AiraHost Report URL: ${reportUrl}
2. Click on the calendar tile for date ${comp.targetDate}
3. Click 'View' on the comparable listing card for "${comp.title}"
`;
          appendReport(reportPath, `### Checking Comp: ${comp.compUrl} [From Entry: ${comp.title}]
**Misalignments Found:**
${errors.join('\n')}
${reproduceSteps}
Screenshot saved to ${screenshotPath}
HTML saved to ${htmlPath}

`);
          anyErrorsFound = true;
        } else {
          
        }
      } catch (e: any) {
        appendReport(reportPath, `**Failed to load comp URL:** ${comp.compUrl}
AiraHost Report URL: ${reportUrl}
Error: ${e.message}

`);
        anyErrorsFound = true;
      }
  }

  if (!anyErrorsFound && compsToTest.length > 0) {
      appendReport(reportPath, `\n✅ All checks passed! No pricing misalignments found.\n\n`);
  }
}


async function setStepperValue(page: Page, label: string, targetValue: number) {
  const field = page.locator('label').filter({ hasText: new RegExp(`^${label}$`) }).locator('xpath=..');
  const value = field.locator('xpath=.//button[normalize-space()="-"]/following-sibling::*[1]');

  await expect(field).toBeVisible();

  for (let i = 0; i < 50; i++) {
    const currentValue = Number(await value.textContent());
    if (currentValue === targetValue) return;

    await field.getByRole('button', { name: currentValue < targetValue ? '+' : '-' }).click();
  }

  throw new Error(`Could not set ${label} to ${targetValue}`);
}

function appendReport(filePath: string, text: string) {
  fs.appendFileSync(filePath, text);
}
