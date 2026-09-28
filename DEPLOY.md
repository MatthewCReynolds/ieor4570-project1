# Deploying Domestique to Cloud Run (continuous deploy from GitHub)

Everything below is done once. After that, every push to `main` rebuilds and redeploys.
Total time: about 20 minutes. Costs: Cloud Run's free tier covers a course project; Gemini Flash-Lite
calls are fractions of a cent.

## 1. Create the Google Cloud project

1. Go to https://console.cloud.google.com and sign in (a Columbia or personal Google account both work).
2. Top bar project picker -> **New project**. Name it `domestique` (the generated **project ID** is what
   the app needs; note it, e.g. `domestique-472118`).
3. **Billing** (left menu) -> link a billing account. New accounts get $300 of credit; a card is required
   but the project will stay well inside the free tier.
4. Enable the APIs. Either click through *APIs & Services -> Enable APIs* for each of
   *Vertex AI API*, *Cloud Run Admin API*, *Cloud Build API*, *Artifact Registry API*, or, faster, run
   in a terminal after authenticating:

   ```bash
   ~/google-cloud-sdk/bin/gcloud auth login
   ~/google-cloud-sdk/bin/gcloud config set project YOUR_PROJECT_ID
   ~/google-cloud-sdk/bin/gcloud services enable aiplatform.googleapis.com run.googleapis.com \
       cloudbuild.googleapis.com artifactregistry.googleapis.com
   ```

## 2. Check the model works from your laptop

```bash
~/google-cloud-sdk/bin/gcloud auth application-default login
cd "~/Desktop/Graduate School/IEOR-4570/Project 1"
GOOGLE_CLOUD_PROJECT=YOUR_PROJECT_ID ~/.local/bin/uv run app.py
```

Open http://localhost:8000 and click a sample prompt. You should see tool-call cards and an answer.
If you get `Model call failed: ... 403`, the Vertex AI API is not enabled or billing is not linked.
If you get `404 ... model not found`, set `MODEL=vertex_ai/gemini-3.5-flash` (or another model listed in
the Vertex AI Model Garden) and retry.

## 3. Create the Cloud Run service with continuous deployment

1. Console -> **Cloud Run** -> **Deploy container** -> **Service** (or *Create service*).
2. Choose **Continuously deploy from a repository (source or function)** -> **Set up with Cloud Build**.
3. **Repository provider:** GitHub. Authenticate, install the *Google Cloud Build* GitHub app when asked,
   and pick `MatthewCReynolds/ieor4570-project1`. (Private repo is fine; the app gets access to that repo.)
4. **Branch:** `^main$`. **Build type:** **Dockerfile**, location `/Dockerfile`. Save.
5. **Service name:** `domestique`. **Region:** `us-central1` (or `us-east4`, closer to New York).
6. **Authentication:** **Allow unauthenticated invocations** (the graders open it in a browser).
7. Expand **Container(s), Volumes, Networking, Security**:
   - *Container port:* `8080`.
   - *Variables & Secrets:* add `GOOGLE_CLOUD_PROJECT` = your project ID.
     Optionally `MODEL` = `vertex_ai/gemini-3.5-flash-lite` (the default) or `vertex_ai/gemini-3.5-flash`.
   - *Resources:* 512 MiB memory, 1 CPU is plenty. *Request timeout:* 300 s (routing + several tool
     rounds can take 30-60 s).
   - **Revision scaling: minimum 0, maximum 1.** Sessions live in memory, so one instance keeps a
     conversation on the same process. (Setting minimum to 1 avoids cold starts during grading but costs
     a few dollars a month; optional.)
8. Click **Create**. The first build takes 3-5 minutes. The service URL looks like
   `https://domestique-xxxxxxxxxx-uc.a.run.app`.

## 4. Give the service permission to call Gemini

Cloud Run runs as the *default compute service account* (`PROJECT_NUMBER-compute@developer.gserviceaccount.com`).
It needs the **Vertex AI User** role:

```bash
PROJECT_ID=YOUR_PROJECT_ID
PROJECT_NUMBER=$(~/google-cloud-sdk/bin/gcloud projects describe $PROJECT_ID --format='value(projectNumber)')
~/google-cloud-sdk/bin/gcloud projects add-iam-policy-binding $PROJECT_ID \
  --member="serviceAccount:${PROJECT_NUMBER}-compute@developer.gserviceaccount.com" \
  --role="roles/aiplatform.user"
```

(Console alternative: *IAM & Admin -> IAM*, edit the compute service account, add role *Vertex AI User*.)

## 5. Verify and record the URL

1. Open the service URL. Click **"Plan a road ride from Columbia University..."**. You should see
   `plan_route` and `estimate_ride_effort` cards and an answer.
2. Open the same URL in a private window and ask something else: separate session, separate memory.
3. Put the URL in two places and push:
   - `submission.json` -> `deploy_url`
   - `README.md` -> the **Live agent** line at the top

   ```bash
   git commit -am "Add Cloud Run deploy URL" && git push
   ```

   The push triggers a rebuild; that is expected and harmless.
4. Submit the GitHub repo URL on Courseworks: https://github.com/MatthewCReynolds/ieor4570-project1

## Troubleshooting

| Symptom | Fix |
|---|---|
| Build fails with "no Dockerfile" | In the Cloud Build trigger, build type must be *Dockerfile* with location `/Dockerfile`. |
| Page loads but every chat says `Model call failed: ... PermissionDenied` | Step 4 (Vertex AI User role) or the Vertex AI API is not enabled. |
| `Model call failed: ... NotFound` | The model name is not available in your project/region; set `MODEL` env var to one that is. |
| Tool cards appear but the answer says a service timed out | BRouter / Overpass are public servers and occasionally slow; ask again. |
| Conversation forgets earlier turns | Max instances must be 1 (Cloud Run -> Edit & deploy new revision -> Scaling). |
| Keep it alive until grades are released | Do nothing; Cloud Run scales to zero and wakes on request. Do not delete the project. |
