# Slack HTTP mode setup (Cloud Run)

After deploying this revision, configure the Slack app at https://api.slack.com/apps
for your PRDEI bot. Socket Mode is no longer used.

## 1. Signing secret

1. App → **Basic Information** → **App Credentials** → **Signing Secret** → Show
2. Put it in `env.yaml` as `SLACK_SIGNING_SECRET: "..."`, then redeploy with `--env-vars-file=env.yaml`
   (or set the env var on the Cloud Run service)

`SLACK_APP_TOKEN` (xapp-...) is unused in production HTTP mode and can stay or be removed.

## 2. Request URLs

Use your Cloud Run HTTPS URL (example):

`https://slackreceiver-312720759301.us-west1.run.app/slack/events`

Set that same URL in all three places:

| Slack setting | Where |
|---------------|--------|
| **Event Subscriptions** → Enable Events → Request URL | `/slack/events` |
| **Interactivity & Shortcuts** → Request URL | `/slack/events` |
| **Slash Commands** → `/logtime` (and any others) → Request URL | `/slack/events` |

Click **Retry / Verify** until Slack shows a green check. The service must be deployed and reachable first.

### Event Subscriptions (bot events)

Subscribe to bot events you need. For this app, interactivity + slash commands carry most traffic. If you use any Events API features, add them here. At minimum, enable Event Subscriptions and verify the URL even if the subscribed list is empty or minimal—slash/interactivity still use their own Request URLs.

## 3. Turn off Socket Mode

**Socket Mode** → toggle **Off** (so Slack does not expect a websocket).

## 4. Deploy

```powershell
gcloud run deploy slackreceiver --source . --region us-west1 --project prdei-ai-sandbox --env-vars-file=env.yaml --memory 2Gi --min-instances 1 --max-instances 1 --no-cpu-throttling
```

Min instances = 1 is still recommended so the hourly reminder thread keeps running. With HTTP mode you no longer need Socket Mode for `/logtime` reliability; you can later lower min instances and move reminders to Cloud Scheduler.

## 5. Smoke test

1. `/logtime` opens the modal  
2. **Open Time Entry Form** on the hourly DM works  
3. Cloud Run logs show Slack POSTs to `/slack/events`, not `BrokenPipeError` reconnect spam  
