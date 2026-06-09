import { test, expect, Page } from '@playwright/test';
import fs from 'fs';
import path from 'path';

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
    const pollResponse = await page.context().request.get(reportApiUrl);
    if (!pollResponse.ok()) {
      return `http:${pollResponse.status()}`;
    }
    reportData = await pollResponse.json();
    return reportData.status;
  }, {
    intervals: [3000],
    timeout: 600000,
    message: `Report did not finish for ${JSON.stringify(input)}`,
  }).toMatch(/^(ready|error)$/);

  if (reportData.status === 'error') {
    appendReport(reportPath, `## Error in analysis\n\nInput: ${JSON.stringify(input)}\nAiraHost Report URL: ${page.url()}\nError: ${reportData.errorMessage}\n\n`);
    return;
  }

  const comps = reportData.resultSummary?.comparableListings || [];
  const reportUrl = page.url();
  appendReport(reportPath, `## Analysis successful\n\nInput: ${JSON.stringify(input)}\nAiraHost Report URL: ${reportUrl}\nFound ${comps.length} comps.\n\n`);

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
1. Go to AiraHost Report URL: ${reportUrl}
2. Open comparable listing: ${compUrl}
3. Compare the scraped data with the live Airbnb page
`;
        appendReport(reportPath, `**Misalignments Found:**\n${errors.join('\n')}\n${reproduceSteps}\nScreenshot saved to ${screenshotPath}\n\n`);
      } else {
        appendReport(reportPath, `All basic checks passed.\n\n`);
      }

    } catch (e: any) {
      appendReport(reportPath, `**Failed to load comp URL:** ${compUrl}\nAiraHost Report URL: ${reportUrl}\nError: ${e.message}\n\n`);
    }
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
