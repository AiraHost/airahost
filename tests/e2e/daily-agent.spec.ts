import { test, expect, Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';

test.describe('Daily Agent - AiraHost Analysis Checker', () => {
  // Use a long timeout because AiraHost analysis might take time,
  // and we are scraping multiple Airbnb URLs.
  test.setTimeout(300000); 

  test('run URL analysis and check comps', async ({ page }) => {
    const urlsToTest = [
      'https://www.airbnb.com/rooms/1596737613274892756',
      'https://www.airbnb.com/rooms/1669685800392899021',
    ];

    const reportPath = path.join(process.cwd(), 'agent_error_report.md');
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
  await page.goto('/tool');

  if (input.mode === 'url') {
    await page.getByRole('button', { name: 'I have a listing URL' }).click();
    await page.getByPlaceholder('https://airbnb.com/rooms/').fill(input.url);
  } else {
    await page.getByRole('button', { name: 'Search by criteria' }).click();
    await page.getByLabel('City *').fill(input.city);
    await page.getByLabel('State *').fill(input.state);
    
    // Set bedrooms
    const bedInput = page.getByLabel('Bedrooms').locator('input').first();
    if (await bedInput.isVisible()) {
      await bedInput.fill(input.bedrooms.toString());
    }
    // Set guests
    const guestsInput = page.getByLabel('Max guests').locator('input').first();
    if (await guestsInput.isVisible()) {
      await guestsInput.fill(input.guests.toString());
    }
  }

  await page.getByRole('button', { name: 'Continue' }).click();

  // Set dates (5 days span)
  if (input.mode === 'criteria' && input.days) {
     const startDateInput = page.getByLabel('Start date');
     const endDateInput = page.getByLabel('End date');
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

  // Wait for the report page to load and API to return
  let reportData: any = null;
  const response = await page.waitForResponse(res => res.url().includes('/api/r/') && res.status() === 200, { timeout: 60000 });
  reportData = await response.json();

  // Wait until status is ready
  while (reportData && reportData.status !== 'ready' && reportData.status !== 'error') {
    await page.waitForTimeout(3000);
    const pollResponse = await page.waitForResponse(res => res.url().includes('/api/r/') && res.status() === 200, { timeout: 60000 });
    reportData = await pollResponse.json();
  }

  if (reportData.status === 'error') {
    appendReport(reportPath, `## Error in analysis\n\nInput: ${JSON.stringify(input)}\nError: ${reportData.errorMessage}\n\n`);
    return;
  }

  const comps = reportData.resultSummary?.comparableListings || [];
  appendReport(reportPath, `## Analysis successful\n\nInput: ${JSON.stringify(input)}\nFound ${comps.length} comps.\n\n`);

  for (const comp of comps.slice(0, 3)) { // Check top 3 to save time
    if (!comp.url) continue;

    const compUrl = comp.url;
    appendReport(reportPath, `### Checking Comp: ${compUrl}\n`);
    
    try {
      await page.goto(compUrl, { waitUntil: 'domcontentloaded' });
      await page.waitForTimeout(3000); // let airbnb render
      
      const screenshotPath = path.join(process.cwd(), `screenshot-${comp.id}.png`);
      await page.screenshot({ path: screenshotPath, fullPage: true });

      // Here we would typically extract text from the page to compare, 
      // but Airbnb's DOM is highly dynamic. We will extract basic title and price.
      const pageTitle = await page.title();
      const pageText = await page.evaluate(() => document.body.innerText);
      
      let errors = [];
      
      // Name aligns
      if (comp.title && !pageTitle.toLowerCase().includes(comp.title.toLowerCase().substring(0, 10))) {
        errors.push(`- Title mismatch: Expected "${comp.title}", got page title "${pageTitle}"`);
      }
      
      // Price scraped aligns
      if (comp.nightlyPrice && !pageText.includes(comp.nightlyPrice.toString())) {
         errors.push(`- Price mismatch: Expected "${comp.nightlyPrice}" but not found in page text.`);
      }
      
      if (errors.length > 0) {
        const reproduceSteps = `
**Way to reproduce:**
1. Go to AiraHost tool (/tool)
2. Submit ${input.mode === 'url' ? 'URL ' + input.url : 'Criteria: Seattle, WA'}
3. Open comparable listing: ${compUrl}
4. Compare the scraped data with the live Airbnb page
`;
        appendReport(reportPath, `**Misalignments Found:**\n${errors.join('\n')}\n${reproduceSteps}\nScreenshot saved to ${screenshotPath}\n\n`);
      } else {
        appendReport(reportPath, `All basic checks passed.\n\n`);
      }

    } catch (e: any) {
      appendReport(reportPath, `**Failed to load comp URL:** ${compUrl}\nError: ${e.message}\n\n`);
    }
  }
}

function appendReport(filePath: string, text: string) {
  fs.appendFileSync(filePath, text);
}
