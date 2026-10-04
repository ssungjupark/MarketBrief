# MarketBrief

Telegram market-close brief for Korean and U.S. markets.

## Scheduling

MarketBrief runs on a Render Cron Job, not GitHub Actions.

One cron service runs twice per weekday (UTC):

- 07:30 UTC Monday-Friday = 16:30 KST Korean close
- 21:30 UTC Monday-Friday = 06:30 KST Tuesday-Saturday U.S. close

The command is:

```bash
uv run --frozen python src/main.py --market AUTO
```

At 07:30 UTC the app resolves to KR. At 21:30 UTC it resolves to US.

## Required Render secrets

Set these in Render. Never commit their values:

- TELEGRAM_BOT_TOKEN
- TELEGRAM_CHAT_ID
- KRX_ID
- KRX_PW

The repository includes `render.yaml`, so Render Blueprint setup creates the cron service and prompts for these four values. `GEMINI_API_KEY` is optional and can be added later in Render if AI-assisted issue summaries are desired.

For KR reports, `REQUIRE_KRX_DATA=true` prevents incomplete KRX flow data from being sent as a successful close report.

GitHub Actions scheduling is intentionally not used.
