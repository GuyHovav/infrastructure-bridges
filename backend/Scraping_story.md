# MnDOT Bridge Portal Scraping — The Engineering Story

## The Goal
Download bridge inspection report PDFs for 2,630 bridges across 5 Minnesota counties
from the MnDOT public reporting portal (`reports.dot.state.mn.us`).

---

## Assumption #1: "It's just a form POST"

The portal URL ends in `.aspx` — classic ASP.NET. The natural first approach was
`requests` + `BeautifulSoup`: scrape the form's hidden fields (`__VIEWSTATE`,
`__EVENTVALIDATION`), POST the bridge number, get a PDF back.

**What actually happened:** The server returned `"Request Rejected"` — a terse
HTML page from an **F5 BIG-IP Web Application Firewall (WAF)**. The WAF validates
session continuity: a raw POST without a prior GET (and the session cookies it
establishes) is blocked outright.

**Fix:** Add a session warm-up step — GET the landing page first to collect the
`ASP.NET_SessionId`, `.ASPXAUTH`, and the F5 `TS0190bb84` WAF cookie. Then POST.

---

## Assumption #2: "Now the POST will redirect to a PDF"

With valid cookies, the WAF passed the request. But the server responded with
`200 OK` and returned… the form page again. No redirect, no PDF.

**Root cause discovered:** The submit button is wired to `rpt.js`, a custom
JavaScript file. Clicking "View Report" does **not** perform a standard HTTP form
POST. Instead, the JavaScript:
1. Submits the form via XHR
2. Receives a response containing a SAP BusinessObjects session token
3. Constructs a BOE OpenDocument URL with that token
4. Opens that URL in a **new browser tab**

`requests` can't execute JavaScript. The redirect never happened because it was
never an HTTP redirect — it was a `window.open()` call.

**Conclusion:** This portal cannot be scraped with `requests` alone.

---

## The Real Solution: Playwright + Network Interception

Switch to **Playwright** (headless Chromium) to execute the JavaScript exactly
as a real browser would. But even then, the challenge wasn't over.

### What the BOE viewer actually does

Once the new tab opens, the SAP BusinessObjects viewer loads two nested frames:
- An outer OpenDocument frame (`openDocument.jsp`)
- An inner Crystal Reports frame (`CrystalReports/view.do`)

The inner frame POSTs to `view.do`, which redirects to the actual report endpoint:

```
CrystalReports/viewrpt.cwr
  ?id=3488660
  &init=html
  &language=en
  &bypassLatestInstance=true
  &cafWebSesInit=true
  &bttoken=<dynamic-session-token>
→ 200 application/pdf
```

The `bttoken` is a **dynamically generated bearer token** — unique per session,
created server-side by SAP BOE. It cannot be predicted or reused.

### The interception strategy

Rather than trying to reconstruct the URL ourselves, we let the browser do all
the work and **intercept the network response** the moment BOE serves the PDF:

```python
def on_response(response):
    if "viewrpt.cwr" in response.url and "pdf" in response.headers.get("content-type", ""):
        captured_pdf_url.append(response.url)  # grab the full URL with bttoken

context.on("response", on_response)  # fires across all pages and frames
```

Once the URL (including the valid `bttoken`) is captured, we re-request it
using `requests` — passing the browser's session cookies — to stream the PDF
to disk with a progress bar.

---

## Final Architecture

```
Playwright (headless Chromium)
  │
  ├─ Fills bridge number in FormDefinition.aspx form
  ├─ Clicks "View Report"
  ├─ rpt.js opens new tab → BOE OpenDocument viewer
  ├─ Crystal Reports frame loads → requests viewrpt.cwr
  └─ Network listener captures: viewrpt.cwr?...&bttoken=XYZ → 200 application/pdf
         │
         ▼
requests.get(captured_url, cookies=playwright_session_cookies)
  └─ Streams PDF to disk: data/raw/pdfs/{structure_number}/inspection_{id}.pdf
```

**Resumability:** Every run checks if the file exists and validates it starts
with PDF magic bytes (`%PDF`) and is >10 KB. Partial/corrupt files from hard
crashes are automatically detected and re-downloaded. The full run of 2,630
bridges can be interrupted and resumed at any point.

---

## Key Lessons

| Layer | What fooled us | What we learned |
|---|---|---|
| **Network** | WAF blocking raw POSTs | Session warm-up (GET before POST) is mandatory |
| **Protocol** | Assumed HTTP redirect | The redirect was `window.open()` — JavaScript only |
| **Architecture** | Assumed direct PDF URL | SAP BOE uses dynamic `bttoken` per session |
| **Solution** | `requests` → Playwright | Use a real browser; intercept, don't reconstruct |

---

## The Interview Punchline

> "The MnDOT portal looked like a standard ASP.NET form. It took three iterations
> to realise it was actually a SAP BusinessObjects Crystal Reports server sitting
> behind an F5 WAF, with the PDF URL gated by a per-session bearer token generated
> by server-side JavaScript. The solution wasn't to fight the stack — it was to
> embrace it: run a real browser, let it do its thing, and intercept the network
> response at the exact moment the PDF appears."
