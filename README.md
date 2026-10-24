# Movies & Television — Reddit RSS

One Feedly-compatible feed, combining **r/movies** and **r/television**, Top (Today), collected by the existing Apify scheduled task.

- Apify task: `notched_trainee/movies-television-top-today` (already runs every eight hours)
- GitHub workflow: publishes from the latest **successful** run at 01:00, 09:00 and 17:00 UTC; GitHub Actions can be delayed.
- Feed URL after GitHub Pages is enabled: `https://pkashe.github.io/movies-television-rss/feed.xml`
- RSS thumbnails appear only when Apify provides a usable image URL; Feedly Classic controls display.

## Setup steps

1. Upload **all files and the `.github/workflows` folder** from this ZIP into the GitHub repository `Pkashe/movies-television-rss`. The repository must be **Public** for free GitHub Pages. This project does not publish private credentials.
2. In the repository select **Settings → Secrets and variables → Actions → New repository secret**. Name it `APIFY_TOKEN` and enter your Apify API token **directly in GitHub**. Do not paste it into chat, files, or commit history.
3. Under **Settings → Pages**, set **Source: GitHub Actions** (not Deploy from a branch).
4. Go to **Actions → Publish Movies and Television RSS → Run workflow**. Verify the workflow passes.
5. Open `https://pkashe.github.io/movies-television-rss/feed.xml` and subscribe in Feedly Classic.

## Important

- **No Cloudflare needed. No NAS needed.** GitHub hosts the RSS feed and runs conversion; Apify runs the Reddit scraper.
- The public GitHub Pages URL is publicly readable. The repository code is public; your API token is not, if kept in Secrets.
- The GitHub workflow only **reads** Apify results; it will not trigger additional paid Apify scrapes.
- A status of FAILED for the generator is deliberate if no fresh successful scraping result is available; it avoids replacing a working feed with empty/stale data.
- If your GitHub username or repository name changes, update `FEED_BASE_URL` in `.github/workflows/publish.yml`.
- Actual thumbnail support depends on the actor returning `imageUrl`, `thumbnailUrl`, `galleryImagesUrls`, or similar image fields.
- The feed contains up to 50 results from the most recent Apify run; it is not a long-term post archive. Your original actor `Maximum Results` setting caps the combined total at 50 and may not distribute exactly 25/25.
- The 8-hour GitHub schedule follows Apify with a 1-hour offset, not an exact completion-triggered webhook.
