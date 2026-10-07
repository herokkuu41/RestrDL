# AI Debug Capture Chrome extension

This is a dependency-free Manifest V3 DevTools extension. It captures diagnostics only
after you press **Start capture** and immediately downloads one JSON file when you press
**Stop & download**.

## Install it in Chrome

1. Open `chrome://extensions`.
2. Turn on **Developer mode**.
3. Click **Load unpacked** and choose this `chrome-extension` folder.
4. Open the page with the problem and press `F12` / Inspect.
5. Open the **AI Capture** panel in DevTools.
6. Press **Start capture**, reproduce the issue, then press **Stop & download**.

The downloaded `ai-debug-capture-*.json` is the file to attach when asking an AI to
investigate the browser-side problem.

## Included data

- HAR-style network requests that finish after Start: request URL, method, status, timing,
  request/response headers, response metadata, failures, and redirects.
- Optional safe text response previews. These are capped at 64 KiB per request.
- Page URL, title, browser environment, navigation/resource timing, capped DOM snapshot,
  console output, JavaScript errors, unhandled promise rejections, and navigation history.

The extension redacts cookies, authorization/API-key headers, and common secret-looking
URL query parameters. It never uploads capture data anywhere. Review a capture before
sharing because URLs and page text may still be sensitive.

## Limits

The export is capped at 8 MiB, includes at most 5,000 requests, and keeps at most a 2 MiB
DOM snapshot. If the capture is too large, the DOM is omitted first and older network
requests are trimmed. This keeps the direct download reliable and the data manageable for
AI analysis.
