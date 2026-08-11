# ond-ci

Reusable GitHub Actions workflow that reviews pull requests with Claude and
submits a formal `APPROVE` / `REQUEST_CHANGES` review, using a GitHub App
(`ond-ai-reviewer`) so the review counts toward branch protection's required
approving review.

## Usage

In the calling repository, add `.github/workflows/ai-review.yml`:

```yaml
name: AI Review

on:
  pull_request:
    types: [opened, synchronize, reopened]

jobs:
  review:
    uses: RenanFR/ond-ci/.github/workflows/ai-pr-review.yml@main
    secrets: inherit
```

The calling repo needs these secrets:

- `APP_ID`: the `ond-ai-reviewer` GitHub App ID
- `APP_PRIVATE_KEY`: the App's private key (PEM)
- `ANTHROPIC_API_KEY`: Anthropic API key

The review criteria (correctness bugs, security issues, and violations of the
repo's `CLAUDE.md` conventions block approval; style nits do not) live in
`scripts/review.py`.
