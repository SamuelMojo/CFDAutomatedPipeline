# AWS side: cfd.samuelojo.tech

A private web front end for the CFD pipeline. People you approve sign in, upload up to five
STEP wings at once, watch each run's progress during the day, and read the results (numbers,
pictures, video). The simulations themselves run on **your PC**, only while the worker
program is open. If anything fails, you get an email.

How it was built, what protects it and what the tests cover is in
[`docs/website-deployment.md`](../docs/website-deployment.md); this page is the working manual.

## How it fits together

```
Browser ──HTTPS──> CloudFront ──> private S3 bucket (the web pages)
   │
   ├──HTTPS──> Cognito ................ sign-in (invite-only)
   ├──HTTPS──> API Gateway ──> Lambda . create runs, list progress, cancel, request access
   ├──HTTPS──> S3 "uploads" bucket .... the zipped wings go straight here (signed upload form)
   └──HTTPS──< S3 "results" bucket .... pictures and video come from here (short-lived links)

DynamoDB: one table of runs, one of access requests
EventBridge ─ every 10 min ─> Lambda "monitor" ... looks for a stuck queue or a silent worker
SNS ─────────────────────────────────────────────> your email: every alert comes through here

Your PC:  aws/worker/worker.py  ──outgoing connections only──>  DynamoDB + S3 + SNS
          takes the oldest queued run -> src/run_job.py (OpenFOAM) -> ParaView pictures -> uploads results
```

Nothing connects *into* your PC. The worker asks AWS "is there a job?" every 15 seconds.

## What a visitor sees

1. **Request access** on the sign-in page. You get an email with the person's details and the
   one command that approves them. Approving creates their account; Cognito emails them a
   temporary password and they choose their own on first sign-in.
2. **Start new runs**: choose 1 to 5 STEP files (or drag them in). For each wing: a name, the
   units it was drawn in, and an optional half-domain mesh. For the whole upload: turbulence
   model (Spalart-Allmaras or k-omega SST), quality (quick or standard), speed and ground.
   FluidX3D is shown as "coming later".
3. STEP files are **zipped in the browser** before upload (a ready-made zip with exactly one
   STEP file is also accepted). Each wing becomes its own run.
4. **Your runs** updates by itself every 8 seconds while anything is unfinished: queue
   position, a seven-step checklist, iteration and cell counts, then Cd, Cl, CdA, ClA, the
   quality checks, pictures, the 360-degree video and downloads. People can close the page and
   come back later; the runs carry on.

## What you get emailed

Every alert is an email from the `cfd-pipeline-admin` SNS topic, subject starting `[CFD site]`.

| Email | When |
|---|---|
| Access request from NAME | Someone used the request form (it contains the approve and reject commands) |
| Run failed | The pipeline crashed, timed out, or the worker could not finish it |
| Run stopped by a quality check | The geometry or mesh gates refused the run |
| Run finished with a FAIL check | The run completed but a quality gate says the numbers cannot be trusted |
| Pictures failed for a run | The numbers are fine but ParaView failed |
| Needs attention: worker offline | Runs have waited over 10 minutes and the worker has been silent over 5 |
| Needs attention: stale running | A run has reported nothing for 30 minutes (the worker probably died) |
| The site's API hit an error | The API code raised an unexpected error (emailed at most once per 10 minutes) |
| CloudWatch alarm | A Lambda function crashed outright |

The same problem is not emailed again for 6 hours (10 minutes for API errors), so a long
outage is one email, not a hundred.

## Deploy (first time)

Prerequisites: an AWS identity that is **not the root user**, Terraform 1.6+, the AWS CLI.

```bash
cd aws/terraform
cp terraform.tfvars.example terraform.tfvars   # then edit it: put YOUR alert address in. Git ignores this file
terraform init -upgrade              # new in this step: the "archive" provider that zips the Lambda code
terraform validate
terraform plan                       # read it: new tables, buckets, API, functions; the site files change
terraform apply
```

Then:

1. **Click the confirmation link** in the email AWS sends from `no-reply@sns.amazonaws.com`.
   Until you do, no alert reaches you.
2. Clear the CloudFront cache so the new pages show straight away:
   ```bash
   aws cloudfront create-invalidation --distribution-id "$(terraform output -raw cloudfront_distribution_id)" --paths "/*"
   ```
3. Create the worker's access key (it is **not** in Terraform, so the secret never lands in the
   state file), and store it as a named profile:
   ```bash
   aws iam create-access-key --user-name "$(terraform output -raw worker_user)"
   aws configure --profile cfd-worker      # paste the key pair when asked; region eu-west-2
   ```
   Never paste the key into a chat or commit it. Clear the terminal afterwards. The worker's user can only update run
   records, read uploads, write results and send alerts: it cannot delete anything or touch
   other buckets.
4. Install the two extra Python packages the worker and admin tool use, in the pipeline's venv:
   ```bash
   pip install boto3
   ```

## Run the worker (on the PC with OpenFOAM)

Start it from a WSL shell where OpenFOAM is loaded and the venv is active, the same as for
`run_job.py`:

```bash
python aws/worker/worker.py --init     # once: writes ~/.cfd-worker/worker.json from terraform output
python aws/worker/worker.py --check    # every line should say OK
python aws/worker/worker.py            # leave this running; Ctrl+C stops it cleanly
```

- It only runs jobs while this window is open. Closed window = runs wait in the queue and the
  site says the worker is offline.
- It runs one job at a time, with 12 processors (the core-count test showed 12 is within about
  3% of 16 and leaves the PC usable). Change `processors` in `~/.cfd-worker/worker.json`.
- A run is stopped after 240 minutes. If the program is closed mid-run, that run goes back in
  the queue once; if it is interrupted twice it is marked failed and you are emailed.
- Finished job folders on the PC are deleted after 14 days; the big `processor*` copies are
  deleted once a run's results have been made.
- To stop Windows sleeping while a long batch runs: Settings > System > Power > Screen and
  sleep > "Never" when plugged in.

`python aws/worker/worker.py --once` does a single queued job and stops: handy for the first test.

### Before strangers use it

A run executes as **your Linux user**, so a deliberately hostile STEP file would meet your
permissions. The worker already starts the pipeline without AWS keys, caps the zip's unpacked
size and applies a time limit, but for people you do not know the right step is to run the worker
as a separate unprivileged Linux user that can read the OpenFOAM install and the repo, but has
nothing else (no SSH keys, no personal files). That is not set up yet; for your own team and
people you have vetted it is a reasonable risk, for the open internet it is not.

## Day to day

```bash
python aws/tools/cfd_admin.py requests              # who is waiting for access
python aws/tools/cfd_admin.py approve them@x.com    # create the account (Cognito emails the password)
python aws/tools/cfd_admin.py approve them@x.com --resend   # new temporary password
python aws/tools/cfd_admin.py reject them@x.com     # decline (nothing is sent to them)
python aws/tools/cfd_admin.py jobs --status failed  # recent runs of everyone
python aws/tools/cfd_admin.py worker                # is the worker checking in?
```

## Limits (all in `variables.tf`)

| Limit | Default |
|---|---|
| Wings per upload | 5 |
| Zip size / unpacked STEP size | 100 MB / 100 MB |
| Unfinished runs per person | 10 |
| New runs per person per 24 hours | 20 |
| Uploads kept in S3 | 14 days |
| Results kept in S3 | 90 days |
| Request-access form | 1 request per second sustained, burst 3 |

The site checks every upload again on the server (is it a real zip, one STEP file, not a zip
bomb) and the worker checks it a third time before unpacking.

## Cost

Everything here is pay-per-use and idle most of the time (Lambda, DynamoDB on demand, API
Gateway, S3, CloudFront). At this scale it should be a few pounds a month at most, dominated by
storage of results and any large downloads. Set a budget alarm anyway: AWS console > Billing >
Budgets > create a monthly cost budget with an email alert.

## Changing the site

Edit files in `web/`, then `terraform apply` and the cache invalidation above. Changing a
Lambda (`lambda/*.py`) also needs only `terraform apply`: Terraform zips and uploads it.

## Terraform state

Terraform records what it has built in `aws/terraform/terraform.tfstate`. It stays on this PC and git ignores it, so
**back it up** now and then (copy it somewhere private): without it Terraform cannot manage the resources it made.
Before anyone else works on this, move it to an S3 backend with locking.

## Tests

From the repository root (works in the pipeline's venv; the browser test needs Playwright and
Node and is skipped without them):

```bash
python -m pytest aws/tests
```

- `test_common`, `test_api`, `test_monitor`, `test_admin`, `test_worker`: the site's rules, the
  API, the health check, the admin tool and the worker, against in-memory stand-ins for AWS.
- `test_web_zip`: the browser's zip maker, read back with Python's zip reader and `unzip`.
- `test_web_ui`: the real pages in headless Chromium, under the production security policy,
  talking to the real API code (sign-in, request access, five-wing upload, live progress,
  results, sign-out, expired sessions, phone layout).

What the tests cannot prove: the first `terraform apply` of the new pieces, real DynamoDB and
S3 behaviour, and the worker driving the real OpenFOAM and ParaView. Do one real run end to end
(`worker.py --once`) before inviting anyone.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| Page shows the old version | Run the CloudFront invalidation, or wait 5 minutes |
| "Could not reach the server" on the runs page | The API URL in `config.js` is wrong or the API is down: `terraform output api_url` |
| Upload fails with "refused" | The upload form expired (15 minutes) or the browser blocked S3: check the browser console for CORS or policy errors |
| Run stays "Queued" | The worker is not running, or it cannot reach AWS: `python aws/worker/worker.py --check` |
| No alert emails | The SNS confirmation link was not clicked, or the email went to junk |
| `--check` fails on ParaView | Set `pvbatch` in `~/.cfd-worker/worker.json` to the Windows path as seen from WSL |
| A run "failed" at the pictures step | The numbers are saved; look at `render.log` in that run's folder under `$FOAM_RUN/web-jobs` |
