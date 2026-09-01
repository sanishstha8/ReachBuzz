## What this changes

<!-- One or two sentences. What behaviour is different afterwards? -->

## Why

<!-- The problem being solved, not the diff restated. -->

## How to verify

<!-- Commands, URLs, or steps a reviewer can actually run. -->

```bash
pytest
```

## Checklist

- [ ] `pytest` passes
- [ ] `ruff check .` passes
- [ ] `python manage.py makemigrations --check --dry-run` reports no changes
- [ ] No credential, token or real phone number appears in the diff
- [ ] Consent rules unchanged, or the change is deliberate and explained below

## Messaging-policy impact

<!-- Required if this touches audience resolution, consent, templates, rate
     limiting or sending. Say "none" otherwise.

     Reminder: an audience is always filtered through
     Contact.objects.eligible(). Nothing may bypass it. -->

none
