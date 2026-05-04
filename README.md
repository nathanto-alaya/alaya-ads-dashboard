# Alaya Ads Dashboard

A free, single-page dashboard that renders Meta Ads performance for Alaya Property and pushes a summary to Slack on demand.

## Stack

- Static HTML/CSS/JS (no backend, no build step)
- GitHub Pages for hosting
- Slack incoming webhook for notifications

## Daily flow

1. Each morning, ask Claude: "Update the ads dashboard"
2. Claude pulls fresh data from Meta Ads, regenerates `data.json`, and pushes to this repo
3. Open the live dashboard in your browser
4. Click "Send to Slack" - a summary is posted to your team channel with the dashboard link

## Files

- `index.html` - the dashboard page (loads `data.json` at runtime)
- `data.json` - the day's numbers (regenerated daily)
- `.nojekyll` - tells GitHub Pages to skip Jekyll processing

## What the dashboard shows

- **Yesterday** - spend, results, cost per result, CTR, vs day-prior deltas
- **Last 7 days** - daily spend bar chart, weekly totals, average CPR
- **Month to date** - running spend, results, pacing
- **Top and bottom performers** - 5 best and 5 worst ads from the last 7 days
- **Campaigns** - all active campaigns with status and budget

## Slack webhook

Wired to: `https://hooks.slack.com/services/T07SCAZCLAY/B0B1SE4UZUZ/...`

The webhook URL is embedded in `index.html`. To rotate it, edit the `SLACK_WEBHOOK` constant at the top of the script tag.

## Status

- Template: complete
- Live data: starts populating 2 June 2026 (current data feed monthly limit resets then)
