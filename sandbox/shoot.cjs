// Screenshot a page and report what went wrong on it. Called by /usr/local/bin/shoot,
// which holds the one-browser lock and the time limit; see that file for usage.
const fs = require("fs");
const path = require("path");
const { chromium } = require("playwright");

const DESKTOP = { viewport: { width: 1280, height: 800 } };
// A common phone width; touch on, so tap targets and hover-only UI show up as they would.
const PHONE = { viewport: { width: 390, height: 844 }, isMobile: true, hasTouch: true, deviceScaleFactor: 1 };

function usage(msg) {
  if (msg) console.error(`shoot: ${msg}`);
  console.error("usage: shoot <url-or-file> <out.png> [--mobile] [--full]");
  process.exit(64);
}

function target(arg) {
  if (/^https?:\/\//.test(arg) || arg.startsWith("file://")) return arg;
  let p = path.resolve(arg);
  if (!fs.existsSync(p)) usage(`no such file: ${arg}`);
  if (fs.statSync(p).isDirectory()) p = path.join(p, "index.html");
  if (!fs.existsSync(p)) usage(`no index.html in ${arg}`);
  return "file://" + p;
}

async function main() {
  const args = process.argv.slice(2);
  const flags = new Set(args.filter((a) => a.startsWith("--")));
  const pos = args.filter((a) => !a.startsWith("--"));
  for (const f of flags) if (f !== "--mobile" && f !== "--full") usage(`unknown option ${f}`);
  if (pos.length !== 2) usage();
  const url = target(pos[0]);
  const out = path.resolve(pos[1]);

  const browser = await chromium.launch({
    headless: true,
    args: ["--headless", "--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu",
           "--js-flags=--max-old-space-size=384"],
  });
  const problems = [];
  try {
    const context = await browser.newContext(flags.has("--mobile") ? PHONE : DESKTOP);
    const page = await context.newPage();
    page.on("console", (m) => {
      if (m.type() === "error" || m.type() === "warning") problems.push(`console.${m.type()}: ${m.text()}`);
    });
    page.on("pageerror", (e) => problems.push(`uncaught: ${e.message}`));
    page.on("requestfailed", (r) => problems.push(`request failed: ${r.url()} (${r.failure()?.errorText})`));
    page.on("response", (r) => { if (r.status() >= 400) problems.push(`HTTP ${r.status()}: ${r.url()}`); });

    await page.goto(url, { waitUntil: "load", timeout: 30000 });
    // Let fonts, late scripts and first animations settle, but don't wait on a busy page forever.
    await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
    await page.waitForTimeout(500);
    await page.screenshot({ path: out, fullPage: flags.has("--full") });

    const size = fs.statSync(out).size;
    const vp = page.viewportSize();
    console.log(`screenshot: ${out} (${vp.width}x${vp.height}${flags.has("--full") ? ", full page" : ""}, ${size} bytes)`);
    // Sideways scrolling on a phone is almost always a bug.
    const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
    if (overflow > 0) problems.push(`layout: page is ${overflow}px wider than the viewport (sideways scroll)`);
  } finally {
    await browser.close();
  }
  if (problems.length) {
    console.log(`problems (${problems.length}):`);
    for (const p of problems) console.log(`  ${p}`);
  } else {
    console.log("problems: none");
  }
}

main().catch((e) => {
  console.error(`shoot: ${e.message.split("\n")[0]}`);
  process.exit(1);
});
