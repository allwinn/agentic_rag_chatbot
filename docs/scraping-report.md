# Scraping report

| Site | Records | Method |
| --- | --- | --- |
| DarGlobal | 38 projects + 14 location guides | Plain HTTP for the listing page; a real Chrome session for the project pages |
| Wasalt | 957 listings + 16 projects | Plain HTTP over search pages (Riyadh and Jeddah, villas and apartments) |

## DarGlobal

- The site is built with Next.js, and each page carries its data as JSON in `__NEXT_DATA__`. One listing page contains all 38 projects.
- Project pages are protected by **Imperva Incapsula**. Plain HTTP and a headless browser both got a challenge page instead of content.
- **What worked:** Playwright connected to a normal Chrome session (over CDP), visiting the pages slowly. All 38 pages were collected.

## Wasalt

- Also Next.js, with the same `__NEXT_DATA__` approach. No bot protection was encountered.
- The scraper builds search URLs by city, property type and page number. It collected 8 pages per search.
- Owner and agent names and contact details are **not stored** (Saudi PDPL).

## Data notes

- DarGlobal pages rarely list unit prices, so prices are not stored for DarGlobal.
- Duplicate DarGlobal pages and non-residential Wasalt listings are removed during indexing.

## Responsible scraping

Public pages only, low volume with delays between requests, and no proxies, stealth tools or CAPTCHA solving.
