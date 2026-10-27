// Renders every avatar SVG here to a 512×512 PNG (what Paperclip stores).
// Usage: node agents/avatars/render.js   (needs Playwright's Chromium)
const { chromium } = require('playwright');
const fs = require('fs');
const path = require('path');

(async () => {
  const browser = await chromium.launch();
  const page = await browser.newPage({ viewport: { width: 512, height: 512 } });
  for (const f of fs.readdirSync(__dirname).filter((f) => f.endsWith('.svg'))) {
    const svg = fs.readFileSync(path.join(__dirname, f), 'utf8');
    await page.setContent(`<body style="margin:0">${svg}</body>`);
    await page.locator('svg').screenshot({ path: path.join(__dirname, f.replace('.svg', '.png')) });
    console.log('rendered', f);
  }
  await browser.close();
})();
