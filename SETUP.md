# Reel robot: setup (about 20 minutes, once)

**What it does:** a free robot on GitHub posts two Instagram Reels a day (one per slot), even when your Mac is
asleep. It wakes up three times an hour, checks if a slot's Reel is due, and posts it. The same runs also save
your follower and Reel numbers (`state/stats.json`) and move trending topics up the queue (`state/order.json`). The Mac keeps answering
comments and DMs when it is awake, but it no longer posts Reels itself.

It starts in **practice mode** (DRY-RUN): it checks everything and writes what it *would* post, but posts nothing.
It only really posts once you switch `LIVE` to `true` (step 9).

---

## 1. Log in to GitHub
Open https://github.com and log in with your new account.

## 2. Make a box (repository)
1. Go to https://github.com/new
2. **Repository name:** `reel-robot`
3. Choose **Public** (private repos hit a GitHub Actions bug in 2026; the token stays a locked secret either way).
4. Do **not** tick "Add a README". Click **Create repository**.

## 3. Make the schedule on the Mac
Open **Terminal** and paste:
```
cd "/Users/preet/Automation Bots/autopilot" && .venv/bin/python -m autopilot cloud export
```
This writes `cloud-poster/schedule.json` (the list of videos, days and captions). Never edit it by hand.

## 4. Send the folder to GitHub
In Terminal, paste these one by one (put your GitHub username where it says `YOURNAME`):
```
gh auth login
cd "/Users/preet/Automation Bots/cloud-poster"
git init -b main
git add .
git commit -m "Reel robot"
git remote add origin https://github.com/YOURNAME/reel-robot.git
git push -u origin main
```
For `gh auth login` pick: GitHub.com, HTTPS, "Login with a web browser", and follow the steps.

## 5. Put the Instagram token in the safe (secret)
1. On GitHub open your `reel-robot` page, click **Settings** (top), then on the left
   **Secrets and variables > Actions**.
2. On the **Secrets** tab click **New repository secret**.
3. **Name:** `IG_TOKEN`
4. **Secret:** paste the Instagram system-user token (the long one starting with `EAA`).
5. Click **Add secret**.

Paste the token **only** into that box. Never into a file, a chat, an email or the variables tab.
GitHub locks it away: nobody (not even you) can read it back, and it is hidden in every log.

## 6. Add the switches (variables)
Same page, click the **Variables** tab, then **New repository variable**, three times:

| Name | Value |
|---|---|
| `IG_USER_ID` | `17841457478590795` |
| `LIVE` | `false` |
| `PAUSED` | `false` |

## 7. Upload the videos
In Terminal:
```
cd "/Users/preet/Automation Bots/cloud-poster" && ./upload_videos.sh
```
It uploads the 50 videos (about 1.5 GB) to the "videos" release. It can take a while. If it stops,
just run it again: videos already uploaded are skipped. At the end it says `Done: ... missing 0`.

## 8. Run the robot once by hand
1. On GitHub click the **Actions** tab. If asked, click **I understand my workflows, go ahead and enable them**.
2. On the left click **Post daily Reel**, then the **Run workflow** button, then the green **Run workflow**.
3. Wait about a minute and click the new run. Scroll down to the **summary**.

A green tick and lines like `DRY-RUN: would post wd-102 now` (or `wd-102 goes out after today's slot ...`)
mean everything works. A red cross: read the summary, it says what to fix.

From now on it runs by itself three times an hour, 13:05-02:45 UTC (14:05-03:45 Italy time in winter), and posts
at the first run after each slot: weekdays 12:00 and 19:00 New York time (18:00 and 01:00 Italy in winter),
weekends 10:00 and 18:00 (16:00 and 00:00 Italy). GitHub sometimes skips a run; the next one, about 20 minutes
later, posts instead. You do not need the Mac for it.

## 9. Go live (only when you decide)
Settings > Secrets and variables > Actions > **Variables** > `LIVE` > edit > type `true` > **Update variable**.
The next run after the slot time in the schedule posts the Reel for real (the run summary always shows
the next slot, in Italy time).

## Everyday switches
* **Pause everything:** set `PAUSED` to `true`. Back on: `false`.
* **Back to practice mode:** set `LIVE` to `false`.
* **Stats off / trend order off:** add a variable `STATS` = `off` or `TRENDS` = `off` (`TRENDS=off` = plain plan
  order). Delete the variable (or set `on`) to turn it back on.
* **Views, reach, saves, shares:** need the `instagram_manage_insights` permission on the token. Until the new token
  is in `IG_TOKEN`, `state/stats.json` says "stats unavailable: needs insights permission" (followers and likes
  still come in) and posting is not affected.
* **Schedule changed on the Mac:** run step 3 again, then in Terminal:
  `cd "/Users/preet/Automation Bots/cloud-poster" && git add schedule.json && git commit -m "New schedule" && git pull --rebase && git push`
* **New or re-made videos:** run `./upload_videos.sh` again.

## What the emails mean
GitHub emails you when a run gets a **red cross**. Open it and read the summary:
* *needs a fix* (exit 1): something is set up wrong, e.g. the token stopped working (make a new one and replace
  the `IG_TOKEN` secret), or a video is missing (run `upload_videos.sh`).
* *will retry* (exit 2): Instagram or the internet had a hiccup. The next run tries again by itself.
* *please check Instagram* (exit 3): Instagram did not say clearly whether the Reel went out. Look at the
  account. The robot checks again itself and never posts the same Reel twice.

## Good to know
* One Reel per slot, never two; at most 2 a day (Italy calendar) and at least 3 hours apart. If a slot is missed
  completely (GitHub outage for more than 4 hours), it is skipped: the next slot takes the next Reel and the rest
  move back one slot. Never a burst of catch-up posts.
* Trend order: once a day the robot reads Google Trends and Google News (free, no keys) and lets the best-matching
  of the next 14 videos go first. It never changes captions. If the trend sites can't be reached: plan order.
* The robot writes what it posted into `state/posted.json`, and its numbers into `state/stats.json`,
  `state/trends.json` and `state/order.json` (do not edit them). Your Mac reads them with
  `python -m autopilot growth` or the Telegram `/growth` command.
* Free: this uses no paid minutes (Actions are free for public repos).
