# How a change reaches main

This repository has one maintainer. That fact shapes the merge gate more than anything
else about it, and the shape has a trap in it that is worth writing down, because the way
out of the trap under pressure is to turn the protection off - which is the one response
that costs more than it saves.

## The path a change takes

1. A branch, a pull request against `main`.
2. `auto-merge.yml` arms GitHub's native auto-merge for pull requests the repository
   owner opens against `main`. It never inspects a review verdict; it sets a flag saying
   "merge once everything required is satisfied".
3. Seventeen required status checks run: unit tests across three operating systems and
   five Python versions, integration tests, lint, format, types, production validation,
   and secret scanning.
4. `pr-review.yml` runs the reviewer from the **base** commit, never the pull request's
   copy, so a change cannot alter the reviewer judging it. It posts its review as a
   comment, and submits an approving review only when it found no blocking issue, the
   author is trusted, and its coverage of the change can be confirmed.
5. When the checks pass and that approval exists, auto-merge squashes it in.

## The trap

Four settings that are each defensible alone:

| Setting | Value |
| --- | --- |
| `required_approving_review_count` | 1 |
| `require_last_push_approval` | true |
| `enforce_admins` | true |
| `bypass_pull_request_allowances` | none |

Together they mean the sole maintainer cannot merge their own work under any
circumstance. The first requires an approval. The second makes the author's own approval
invalid for their most recent push. The third removes the admin override. The fourth
leaves no configured exception. Every route is closed except the bot's approval, and the
bot is a language model reading a diff - it declines sometimes, and it is wrong
sometimes. On 2026-08-01 it blocked a change to itself by reporting the phrase "ignore
prior instructions" inside its own prompt as an injection attempt.

At that point there is no legitimate way to merge anything, and the reachable option is
to switch branch protection off, push, and switch it back on. That is what happened here
before this document existed, and it is worth being precise about why it is the wrong
tool rather than merely inelegant:

- **It disables everything, not the one rule in the way.** For the length of that window
  the required checks, the force-push protection and the deletion protection are all off,
  for every actor, not only for the person who turned it off.
- **It leaves no record.** A commit that landed that way is indistinguishable afterwards
  from one that passed the full gate. This repository's history contains commits with no
  pull request number, and nothing now says which were which.
- **It trains the reflex.** The next time is easier, and the time after that is a habit.

## What to do instead

Configure the exception rather than removing the wall. A bypass allowance waives the
review requirement, and only that, for a named actor:

```bash
gh api -X PATCH repos/OWNER/REPO/branches/main/protection/required_pull_request_reviews \
  --input - <<'JSON'
{
  "required_approving_review_count": 1,
  "require_last_push_approval": true,
  "dismiss_stale_reviews": true,
  "bypass_pull_request_allowances": { "users": ["OWNER"] }
}
JSON
```

The seventeen required checks still have to pass. Force-push and deletion protection stay
on. The pull request still exists with its diff, its checks and the reviewer's comment
attached, so the record survives; what changes is only that the maintainer can merge when
the bot cannot approve.

`enforce_admins: false` with `gh pr merge --admin` is the other way, and is worse for
this purpose: an admin merge bypasses the required status checks too, so it waives the
part that actually catches defects along with the part that is in the way.

## What must not be relaxed to make this easier

The review requirement is the weakest thing in the gate here and the checks are the
strongest, which is the opposite of how it is usually described. One person cannot
meaningfully review their own work, and a language model's approval is a useful signal
rather than a second pair of eyes. What actually catches defects in this repository is
the matrix of tests, the type checker, the formatter and the production validation - and
none of that can be satisfied by an opinion.

So if pressure comes to loosen something:

- **Keep every required status check.** They are the gate.
- **Keep force-push and deletion protection.** They protect history, which is the one
  thing that cannot be reconstructed.
- **Relax the review requirement**, through a bypass allowance, because it is the rule
  that has no honest way to be satisfied by one person.

`require_code_owner_reviews` is currently `true` with no `CODEOWNERS` file anywhere in
the repository, which means it protects nothing today. Adding a `CODEOWNERS` naming the
maintainer would not add protection either - it would add a second copy of the same trap,
since the owner cannot approve their own pull request. Either leave it inert or turn it
off; do not satisfy it by adding a file.

## The reviewer's own bootstrap

`pr-review.yml` running the reviewer from the base commit is correct and should stay.
Its consequence is that the reviewer is the one thing in the repository whose behaviour
cannot be observed before it takes effect: a change to it is judged by the version
already on `main`, and only governs the next pull request after it merges.

Two things follow. Land reviewer changes as their own small pull request, so the current
reviewer can read all of it rather than a truncated fraction. And use
`review-preview.yml` to run a candidate reviewer against a real pull request first: it
decides nothing, holds no approval permission, and is the only way to see what a
reviewer change does before it is doing it.
