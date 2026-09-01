# Contributing

## Branches

| Branch | Purpose |
|---|---|
| `main` | Stable. Only ever updated by merging `develop`. Tagged at each milestone. |
| `develop` | Integration branch. Feature branches merge here first. |
| `feature/<name>` | One branch per piece of work. Branch from `develop`, merge back to `develop`. |
| `fix/<name>` | Bug fixes. Same flow. |
| `hotfix/<name>` | Urgent production fix. Branch from `main`, merge to **both** `main` and `develop`. |

```bash
git switch develop
git pull                              # once a remote exists
git switch -c feature/what-it-does
# ...work...
git switch develop && git merge --no-ff feature/what-it-does
```

`--no-ff` keeps the feature's commits grouped, so the history shows what
shipped together rather than a flat line.

A pre-commit hook refuses direct commits to `main`. That is deliberate: `main`
should only ever change through a merge that CI has checked.

## Before you push

```bash
pytest                                          # all tests
ruff check .                                    # lint
python manage.py makemigrations --check --dry-run   # no missing migrations
```

CI runs exactly these, plus `manage.py check --deploy` against the production
settings. Install the hooks once and they run automatically:

```bash
pip install pre-commit
pre-commit install
```

## Commit messages

```
<type>(<scope>): <what changed, imperative, lower case>

<why it changed, and any consequence a reader would not guess>
```

Types: `feat`, `fix`, `refactor`, `perf`, `test`, `docs`, `chore`, `build`, `ci`.

The body is the valuable part. Explain the reasoning, not the diff — a reader
can see *what* changed; they cannot see why you rejected the obvious
alternative.

## Rules this project does not bend

These exist because the system sends messages to real people.

1. **Consent is never inferred.** An audience is resolved through
   `Contact.objects.eligible()`, which has no override parameter. If you add a
   sending path, it goes through that function. CSV import marks a contact
   opted-in only on an explicitly affirmative consent value.

2. **Consent changes are audited.** Use `contacts.services.set_consent()`. Do
   not write `Contact.opted_in` directly — a second, unaudited path defeats the
   compliance trail.

3. **Credentials live in the environment.** Never in code, a fixture, a log
   line, an error message, or an API response. `core.logging_filters` is a last
   line of defence, not permission to be careless.

4. **Template approval belongs to Meta.** Nothing in this application may mark
   a template approved or submit one for review. Local templates are for
   development and are refused under the live provider.

5. **Rate limiting throttles us, never evades them.** Honour `Retry-After`.
   Do not add number rotation, retry storms, or anything whose purpose is to
   exceed what the provider permits.

6. **Campaign state moves through the state machine.** Use
   `campaigns.services.transition()`. It exists to stop things like relaunching
   a completed campaign, which would message everyone twice.

If a change needs to bend one of these, say so explicitly in the PR's
messaging-policy section rather than doing it quietly.

## Tests

Every test must pass without Meta credentials, without Redis, and without a
network. The test settings enforce this: the mock provider is forced on, Celery
runs inline, and rate limiting is disabled.

New behaviour needs a test that would fail without it. For anything touching
consent, sending or state transitions, also add the test for the case you are
*preventing* — the send that must not happen.
