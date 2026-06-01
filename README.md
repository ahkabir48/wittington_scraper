# Wittington Scraper — README

**To try the web viewer, visit https://ahkabir48.github.io/wittington_scraper/** (deployment on the right toolbar in this repo).

Optimized for costs and given limited context. 

Total task involved scraping and screening **3288** companies from https://manife.st/who-attends.

## Description

Full sourcing and screening data ingestion pipeline, including:
- **scraper.py**: simple scraper that extracts companies from webpage html and outputs companies.csv. Separate from analysis since it would need to be unique to the webpage being scraped from.
- **ai_review.py**: main pipeline script, (1) reads companies.csv, (2) deterministically eliminates obvious passes, (3) runs Tavily API search for context, (4) calls Fireworks APi with small OpenAI-gpt-oss-20b model that produces a short output describing decision and rationale, writes results into enriched_companies.db for cached data, export to enriched_companies.csv for use in index.html
- **accuracy_check.py**: helper function for testing.
- **index.html**: main web viewer for output csv data
- **enriched_companies.db**: SQL database containing simple data from LLM output

What the web viewr (index.html, access at **https://ahkabir48.github.io/wittington_scraper/**) shows:
- A table of enriched companies with columns: Name (links to Tavily top URL if available), Score (Strong / Potential / Pass), and Rationale (single-line excerpt; full notes viewable on click).
- Filters: free-text, sort by score or name, download/load CSV.


## Cost Breakdown
- Build: $0 --> Github Copilot support in VSCode, utilizing free models available with Student Pack
- Web Search (Tavily): $0 --> 1,000 credits (search)/month with free tier, cycled through 3 accounts to avoid incurring costs here. Assuming I went with *pay as you go* on one account, the cost would've been $0.008 per search past 1,000 searches each month. For this single task that would've resulted in ~$16 (not considering pre-passes due to keywords like "ventures").
- LLM (Fireworks API - OpenAI gpt-oss-20b): $0.65 --> low context model which is more than serviceable for this purpose. Pricing is $0.07/M uncached input tokens, $0.04/M cached input tokens, $0.30/M output tokens. **5.65M tokens** used for this task (both testing and prod), incurring **$0.66** in LLM costs. 
<img width="1172" height="764" alt="image" src="https://github.com/user-attachments/assets/854c256c-7faf-4466-8048-20690f2703f5" />


