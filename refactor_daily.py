import re

with open(r'C:\Users\limue\Documents\Projects\AiraHost\airahost-main\tests\e2e\daily-agent.spec.ts', 'r', encoding='utf-8') as f:
    text = f.read()

# 1. Update report path
text = re.sub(
    r"const reportPath = path\.join\(process\.cwd\(\), 'agent_error_report\.md'\);\n\s*fs\.writeFileSync\(reportPath, '# Daily Agent Error Report\\n\\n'\);",
    r'''const dateStr = new Date().toISOString().split('T')[0];
    const reportDir = path.join(process.cwd(), 'tests', dateStr);
    if (!fs.existsSync(reportDir)) {
      fs.mkdirSync(reportDir, { recursive: true });
    }
    const reportPath = path.join(reportDir, 'agent_error_report.md');
    fs.writeFileSync(reportPath, '# Daily Agent Error Report\\n\\n');''',
    text
)

# 2. Check all 7 days
text = text.replace('for (let i = 0; i < Math.min(2, dateCount); i++) {', 'for (let i = 0; i < dateCount; i++) {')

# 3. Add User-owned listing check logic
user_check_code = '''
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
'''
text = text.replace('appendReport(reportPath, `Collected ${compsToTest.length} comp checks from UI.\\n\\n`);', user_check_code + '\n  appendReport(reportPath, `Collected ${compsToTest.length} checks.\\n\\n`);')

# 4. Modify Price Validation to allow +- 2%
# In the existing code, it has "let found = false;" followed by price checks.
price_logic_old = '''           let found = false;
           // Only exact substring match on the stripped page text
           if (comp.expectedTotal && normalizedText.includes(comp.expectedTotal)) found = true;
           if (comp.expectedPrice && normalizedText.includes(comp.expectedPrice)) found = true;
           // Sometimes Airbnb rounds the decimal, try rounding as a fallback
           if (!found && comp.expectedPrice) {
               const parsedPrice = parseFloat(comp.expectedPrice);
               if (!isNaN(parsedPrice) && normalizedText.includes(Math.round(parsedPrice).toString())) {
                   found = true;
               }
           }
           if (!found && comp.expectedTotal) {
               const parsedTotal = parseFloat(comp.expectedTotal);
               if (!isNaN(parsedTotal) && normalizedText.includes(Math.round(parsedTotal).toString())) {
                   found = true;
               }
           }'''

price_logic_new = '''           let found = false;
           const pagePrices = (pageText.match(/\\$([0-9,]+(?:\\.[0-9]+)?)/g) || []).map(p => parseFloat(p.replace(/[$,]/g, '')));
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
           }'''
text = text.replace(price_logic_old, price_logic_new)

# 5. Make sure it only shows the error in detail, only show checks pass if all dates pass.
# Instead of printing "All basic checks passed for date" inside the loop, we collect errors.
# We need to change the loop to collect overall status.
# We will do this by tracking a boolean flag `allPassed` and writing it at the end.
# Actually, the user says "only show the error in detail, only show checks pass if all dates pass".
success_message_old = '''        if (errors.length > 0) {
          const reproduceSteps = `
**Way to reproduce:**
1. Go to AiraHost Report URL: ${reportUrl}
2. Click on the calendar tile for date ${comp.targetDate}
3. Click 'View' on the comparable listing card for "${comp.title}"
`;
          appendReport(reportPath, `**Misalignments Found:**\\n${errors.join('\\n')}\\n${reproduceSteps}\\nScreenshot saved to ${screenshotPath}\\nHTML saved to ${htmlPath}\\n\\n`);
        } else {
          appendReport(reportPath, `All basic checks passed for date ${comp.targetDate}.\\n\\n`);
        }'''

success_message_new = '''        if (errors.length > 0) {
          const reproduceSteps = `\\n**Way to reproduce:**\\n1. Go to AiraHost Report URL: ${reportUrl}\\n2. Click on the calendar tile for date ${comp.targetDate}\\n3. Click 'View' on the comparable listing card for "${comp.title}"\\n`;
          appendReport(reportPath, `### Checking Comp: ${comp.compUrl} [From Entry: ${comp.title}]\\n**Misalignments Found:**\\n${errors.join('\\n')}\\n${reproduceSteps}\\nScreenshot saved to ${screenshotPath}\\nHTML saved to ${htmlPath}\\n\\n`);
        } else {
          allPassed = true; // wait, let's track per analysis. But for now we just don't append pass for individual.
        }'''
text = text.replace(success_message_old, success_message_new)

# remove the old "Checking Comp" log which prints unconditionally
text = text.replace("appendReport(reportPath, `### Checking Comp: ${comp.compUrl} [From Entry: ${comp.title}]\\n`);", "")

# We need to append "All basic checks passed" at the end of the `compsToTest` loop if no errors occurred across all comps.
# Let's add a global tracker for the current analysis.
# Before `for (const comp of compsToTest) {` we add `let anyErrorsFound = false;`
# Inside the loop, if errors > 0, `anyErrorsFound = true;`
# After the loop, `if (!anyErrorsFound) appendReport(...)`
text = text.replace('for (const comp of compsToTest) {', 'let anyErrorsFound = false;\\n  for (const comp of compsToTest) {')
text = text.replace('errors.join(\'\\n\')}\\n${reproduceSteps}\\nScreenshot saved to ${screenshotPath}\\nHTML saved to ${htmlPath}\\n\\n`);', 'errors.join(\'\\n\')}\\n${reproduceSteps}\\nScreenshot saved to ${screenshotPath}\\nHTML saved to ${htmlPath}\\n\\n`);\\n          anyErrorsFound = true;')
text = text.replace('appendReport(reportPath, `**Failed to load comp URL:** ${comp.compUrl}\\nAiraHost Report URL: ${reportUrl}\\nError: ${e.message}\\n\\n`);', 'appendReport(reportPath, `**Failed to load comp URL:** ${comp.compUrl}\\nAiraHost Report URL: ${reportUrl}\\nError: ${e.message}\\n\\n`);\\n        anyErrorsFound = true;')

end_of_loop_replacement = '''
  if (!anyErrorsFound && compsToTest.length > 0) {
      appendReport(reportPath, `\\n✅ All checks passed! No pricing misalignments found.\\n\\n`);
  }
}
'''
text = text.replace('  }\n}', '  }\n' + end_of_loop_replacement)

with open(r'C:\Users\limue\Documents\Projects\AiraHost\airahost-main\tests\e2e\daily-agent.spec.ts', 'w', encoding='utf-8') as f:
    f.write(text)
print('Updated 2')
